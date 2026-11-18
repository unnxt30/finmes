"""Snapshots for explicit bulk approval; opening and editing never save mappings."""
from __future__ import annotations

import json
import logging
import threading
import uuid
from decimal import Decimal

from finmes.journal import Mappings
from finmes.reviews import collect_unknowns

log = logging.getLogger(__name__)

MIN_CONFIDENCE = .85


class BulkReviews:
    def __init__(self, engine):
        self.engine = engine
        self.lock = threading.RLock()
        with engine.db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS bulk_reviews (
                id TEXT PRIMARY KEY, actor INTEGER NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                state TEXT NOT NULL DEFAULT 'open', items TEXT NOT NULL)""")

    def create(self, actor: int, count: int = 10) -> dict:
        self.engine.authorize(actor)
        if not 1 <= count <= 10:
            raise ValueError("Batch summaries support 1–10 payees. Use individual mode for larger sessions.")
        self.engine.scan()
        candidates = collect_unknowns(self.engine.journal_dir, self.engine.mappings_path)
        candidates.sort(key=lambda c: Decimal(c.get('debit_inr', '0')), reverse=True)
        with self.engine.db() as db:
            available = {r['payee']: r['id'] for r in db.execute(
                "SELECT payee,id FROM reviews WHERE state IN ('waiting','pending')")}
        items = []
        for candidate in candidates:
            if candidate['payee'] not in available:
                continue
            row = self.engine.prepare(available[candidate['payee']])
            if row['state'] != 'pending':
                continue
            items.append(self.snapshot(row, candidate))
            if len(items) >= count:
                break
        if not items:
            raise ValueError('No pending unknown payees to review.')
        batch_id = uuid.uuid4().hex
        with self.engine.db() as db:
            db.execute('INSERT INTO bulk_reviews (id,actor,items) VALUES (?,?,?)',
                       (batch_id, actor, json.dumps(items)))
        return self.get(batch_id, actor)

    @staticmethod
    def snapshot(row, candidate=None, *, manual=False):
        confidence = row['confidence']
        eligible = bool(row['category']) and (manual or (confidence is not None and confidence >= MIN_CONFIDENCE))
        return {'id': row['id'], 'version': row['version'], 'category': row['category'],
                'confidence': confidence, 'included': eligible,
                'candidate': candidate or row['candidate']}

    def get(self, batch_id: str, actor: int, version: int | None = None) -> dict:
        self.engine.authorize(actor)
        with self.engine.db() as db:
            row = db.execute('SELECT * FROM bulk_reviews WHERE id=?', (batch_id,)).fetchone()
        if row is None:
            raise ValueError('Batch no longer exists; run /review again.')
        result = dict(row)
        if result['actor'] != actor:
            raise PermissionError('This review batch belongs to another user.')
        if version is not None and result['version'] != version:
            raise ValueError('This summary is outdated; reopen /review to see current choices.')
        result['items'] = json.loads(result['items'])
        return result

    def update(self, batch_id, actor, index, row, *, include=None, manual=False):
        with self.lock:
            batch = self.get(batch_id, actor)
            if batch['state'] != 'open':
                raise ValueError('This batch is closed; run /review again.')
            old = batch['items'][index]
            if old['id'] != row['id']:
                raise ValueError('Payee does not belong to this batch.')
            item = self.snapshot(row, old['candidate'], manual=manual)
            if include is not None:
                if include and not row['category']:
                    raise ValueError('Choose a category before including this payee.')
                item['included'] = include
            batch['items'][index] = item
            with self.engine.db() as db:
                db.execute('UPDATE bulk_reviews SET items=?,version=version+1 WHERE id=?',
                           (json.dumps(batch['items']), batch_id))
        return self.get(batch_id, actor)

    def close(self, batch_id, actor, version):
        with self.lock:
            batch = self.get(batch_id, actor, version)
            if batch['state'] != 'open':
                raise ValueError('This batch is already closed.')
            with self.engine.db() as db:
                db.execute("UPDATE bulk_reviews SET state='closed',version=version+1 WHERE id=?", (batch_id,))

    def approve(self, batch_id, actor, version):
        with self.lock:
            batch = self.get(batch_id, actor, version)
            if batch['state'] != 'open':
                raise ValueError('This batch is already closed.')
            items = [i for i in batch['items'] if i['included'] and i['category']]
            if not items:
                raise ValueError('No payees are included. Edit a payee to choose a category first.')
            # Check the entire visible snapshot before any financial write.
            mappings = Mappings.load(self.engine.mappings_path)
            for item in items:
                row = self.engine.get(item['id'])
                self.engine._check(row, item['version'])
                if row['category'] != item['category']:
                    raise ValueError('A category changed; reopen /review before approving.')
                previous = mappings.payees.get(row['payee'])
                if previous is not None and previous != item['category']:
                    raise ValueError('A payee mapping changed; reopen /review before approving.')
            saved, failed, changed = [], [], 0
            for item in items:
                try:
                    changed += self.engine.confirm(item['id'], item['version'], actor)
                    saved.append(item['candidate']['payee'])
                except Exception:
                    # Each confirmation has its own backups and transaction. Report partial
                    # completion honestly; retrying the pending payee is idempotent.
                    log.exception("Bulk mapping confirmation did not finish")
                    failed.append(item['candidate']['payee'])
            self.close(batch_id, actor, version)
            return {'saved': saved, 'failed': failed, 'changed': changed,
                    'pending': len(batch['items']) - len(saved)}
