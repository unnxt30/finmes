"""One-payment confirmations and reviewable suggestions for older payments."""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from finmes.categories import categorize_payments
from finmes.enrichment import EnrichmentError, coordinates, decide_payment_matches
from finmes.reviews import _now, collect_unknowns, payment_amount, unknown_payments

log = logging.getLogger(__name__)

MAX_CANDIDATES = 30
MAX_MATCHES = 10
MIN_PROBABILITY = 0.85


def payment_evidence(payment):
    return {key: payment[key] for key in ("payee", "amount", "direction")}


def near_example(payment, example):
    point = coordinates(payment.get("location"))
    reference = coordinates(example.get("location"))
    return bool(point and reference
                and abs(point[0] - reference[0]) <= .001
                and abs(point[1] - reference[1]) <= .001)


class PaymentReviews:
    def __init__(self, engine):
        self.engine = engine
        with engine.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS confirmed_payments (
                    key TEXT PRIMARY KEY, category TEXT NOT NULL, payment TEXT NOT NULL,
                    confirmed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payment_match_reviews (
                    id TEXT PRIMARY KEY, actor INTEGER NOT NULL, category TEXT NOT NULL,
                    matches TEXT NOT NULL, checked INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open', version INTEGER NOT NULL DEFAULT 1
                );
            """)

    def _unknowns(self):
        return (dict(payment, payee=payee)
                for payee, payment in unknown_payments(self.engine.journal_dir, self.engine.mappings_path))

    def _selected_review(self, db, review_id, version):
        row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        self.engine._check(row, version)
        if row["state"] != "pending" or not row["category"]:
            raise ValueError("Choose a category before confirming a payment.")
        return row

    def choices(self, review_id, version, actor):
        self.engine.authorize(actor)
        with self.engine.db() as db:
            row = self._selected_review(db, review_id, version)
        payments = [p for p in self._unknowns() if p["payee"] == row["payee"]]
        if not payments:
            raise ValueError("This payee has no uncategorized payments. Run /review to update the list.")
        payments.sort(key=lambda p: (p["date"], p.get("time", ""), p["key"]), reverse=True)
        return row["payee"], row["category"], payments

    def confirm(self, review_id, version, actor, key):
        self.engine.authorize(actor)
        with self.engine.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._selected_review(db, review_id, version)
            payment = next((p for p in self._unknowns() if p["key"] == key and p["payee"] == row["payee"]), None)
            if not payment or not self._apply(db, [dict(payment, category=row["category"])]):
                raise ValueError("This payment has already changed. Run /review to see the current payments.")
            return payment, row["category"]

    def _apply(self, db, payments):
        changed = categorize_payments(self.engine.journal_dir,
                                      {p["key"]: (p["payee"], p["category"]) for p in payments})
        if not changed:
            return 0
        confirmed_at = _now()
        db.executemany("INSERT OR REPLACE INTO confirmed_payments VALUES (?,?,?,?)", (
            (p["key"], p["category"], json.dumps(payment_evidence(p) | {
                "key": p["key"], "location": p.get("location"),
            }), confirmed_at) for p in payments if p["key"] in changed
        ))
        affected = {p["payee"] for p in payments if p["key"] in changed}
        remaining = {row["payee"]: row for row in
                     collect_unknowns(self.engine.journal_dir, self.engine.mappings_path)
                     if row["payee"] in affected}
        for payee in affected:
            candidate = remaining.get(payee)
            if candidate is not None:
                candidate.pop("keys")
            db.execute("""UPDATE reviews SET candidate=COALESCE(?, candidate), state=?,
                version=version+1, category=NULL, confidence=NULL, places='[]', note='', updated_at=?
                WHERE payee=? AND state IN ('waiting','pending')""",
                       (json.dumps(candidate) if candidate else None, "waiting" if candidate else "resolved",
                        _now(), payee))
        return len(changed)

    def _confirmed_examples(self, category=None):
        with self.engine.db() as db:
            where = " AND category=?" if category is not None else ""
            args = (category,) if category is not None else ()
            for row in db.execute("SELECT category,payment FROM confirmed_payments WHERE 1=1"
                                  + where + " ORDER BY confirmed_at DESC", args):
                yield row["category"], json.loads(row["payment"])
            # Prior approvals labelled the payee. Take one payment per currency
            # and direction from each review to give different payees a voice.
            for row in db.execute("""SELECT payee,candidate,category FROM reviews
                WHERE state='approved' AND approved_by IS NOT NULL"""
                + where + " ORDER BY updated_at DESC", args):
                seen = set()
                for payment in json.loads(row["candidate"])["examples"]:
                    group = payment_amount(payment["amount"])[0], payment["direction"]
                    if group not in seen:
                        yield row["category"], dict(payment, payee=row["payee"], confirmed_by_payee=True)
                        seen.add(group)

    def _score(self, category, examples, payments):
        amounts = [payment_amount(example["amount"])[1] for example in examples]
        ranked = []
        for payment in payments:
            _, value = payment_amount(payment["amount"])
            nearby = any(near_example(payment, example) for example in examples)
            evidence = dict(payment_evidence(payment), near_confirmed_payment=nearby)
            ranked.append((not nearby, min(abs(value - amount) for amount in amounts), payment, evidence))
        ranked.sort(key=lambda item: item[:2])
        shortlist = ranked[:MAX_CANDIDATES]
        if not shortlist:
            return [], set()
        evidence = [dict(payment_evidence(p), confirmation_scope="payee" if p.get("confirmed_by_payee") else "payment")
                    for p in examples]
        scores = decide_payment_matches(self.engine.openai_key, category, evidence,
                                        [entry[3] for entry in shortlist])
        matches = [dict(shortlist[index][2], category=category, probability=probability, included=True)
                   for index, probability in scores.items() if probability >= MIN_PROBABILITY]
        return matches, {entry[2]["key"] for entry in shortlist}

    def _store_matches(self, actor, category, matches, checked):
        if not matches:
            return None
        matches.sort(key=lambda p: p["probability"], reverse=True)
        batch_id = uuid.uuid4().hex
        with self.engine.db() as db:
            db.execute("""INSERT INTO payment_match_reviews (id,actor,category,matches,checked)
                VALUES (?,?,?,?,?)""", (batch_id, actor, category, json.dumps(matches[:MAX_MATCHES]), checked))
        return self.get(batch_id, actor)

    def suggest(self, actor, seed, category):
        self.engine.authorize(actor)
        currency, amount = payment_amount(seed["amount"])
        if amount is None:
            return None, 0
        direction = seed["direction"]
        examples, seen = [seed], {seed["key"]}
        for _, payment in self._confirmed_examples(category):
            if len(examples) == 5:
                break
            example_currency, value = payment_amount(payment["amount"])
            if value is not None and example_currency == currency and payment["direction"] == direction and payment["key"] not in seen:
                examples.append(payment)
                seen.add(payment["key"])
        payments = [p for p in self._unknowns() if p["direction"] == direction
                    and payment_amount(p["amount"])[0] == currency]
        matches, checked = self._score(category, examples, payments)
        return self._store_matches(actor, category, matches, len(checked)), len(checked)

    def suggest_from_history(self, actor, category=None):
        """Review a bounded set of older payments using existing approval history."""
        self.engine.authorize(actor)
        if not self.engine.openai_key:
            raise EnrichmentError("OPENAI_API_KEY is missing")
        payments_by_kind = defaultdict(list)
        for payment in self._unknowns():
            currency, amount = payment_amount(payment["amount"])
            if amount is not None:
                payments_by_kind[currency, payment["direction"]].append(payment)

        groups = defaultdict(dict)
        for account, payment in self._confirmed_examples(category):
            currency, amount = payment_amount(payment["amount"])
            kind = currency, payment["direction"]
            if amount is not None and kind in payments_by_kind:
                examples = groups[account, kind]
                if len(examples) < 5:
                    examples.setdefault(payment["key"], payment)

        by_key = defaultdict(list)
        checked, failures = set(), 0
        with ThreadPoolExecutor(max_workers=3) as pool:
            jobs = [pool.submit(self._score, account, list(examples.values()), payments_by_kind[kind])
                    for (account, kind), examples in groups.items()]
            for job in jobs:
                try:
                    matches, keys = job.result()
                except EnrichmentError as exc:
                    log.warning("Historical Decisions request failed: %s", exc)
                    failures += 1
                    continue
                checked.update(keys)
                for match in matches:
                    by_key[match["key"]].append(match)

        # If two categories are plausible, leave the payment for manual review.
        matches = [options[0] for options in by_key.values() if len(options) == 1]
        return self._store_matches(actor, category or "", matches, len(checked)), len(checked), failures

    def _batch(self, db, batch_id, actor, version=None):
        self.engine.authorize(actor)
        row = db.execute("SELECT * FROM payment_match_reviews WHERE id=?", (batch_id,)).fetchone()
        if row is None or row["state"] != "open":
            raise ValueError("This list of suggested payments is closed.")
        if row["actor"] != actor:
            raise PermissionError("This list belongs to another user.")
        if version is not None and row["version"] != version:
            raise ValueError("Use the latest version of this list.")
        return dict(row, matches=json.loads(row["matches"]))

    def get(self, batch_id, actor):
        with self.engine.db() as db:
            return self._batch(db, batch_id, actor)

    def open_batches(self):
        with self.engine.db() as db:
            return [dict(row, matches=json.loads(row["matches"])) for row in db.execute(
                "SELECT * FROM payment_match_reviews WHERE state='open'") if row["actor"] in self.engine.allowed_users]

    def select(self, batch_id, actor, version, keys):
        with self.engine.db() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._batch(db, batch_id, actor, version)
            for match in batch["matches"]:
                match["included"] = match["key"] in keys
            db.execute("UPDATE payment_match_reviews SET matches=?, version=version+1 WHERE id=?",
                       (json.dumps(batch["matches"]), batch_id))
            batch["version"] += 1
            return batch

    def finish(self, batch_id, actor, version, *, approve):
        with self.engine.db() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = self._batch(db, batch_id, actor, version)
            selected = [dict(match, category=match.get("category", batch["category"]))
                        for match in batch["matches"] if match["included"]]
            changed = self._apply(db, selected) if approve and selected else 0
            db.execute("UPDATE payment_match_reviews SET state='closed' WHERE id=?", (batch_id,))
            return changed, len(selected) - changed
