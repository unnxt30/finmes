"""One-payment confirmations and reviewable suggestions for older payments."""

from __future__ import annotations

import json
import uuid

from finmes.categories import categorize_payments
from finmes.enrichment import coordinates, decide_payment_matches
from finmes.reviews import _now, collect_unknowns, payment_amount, unknown_payments

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
            if not payment or not self._apply(db, [payment], row["category"]):
                raise ValueError("This payment has already changed. Run /review to see the current payments.")
            return payment, row["category"]

    def _apply(self, db, payments, category):
        changed = categorize_payments(self.engine.journal_dir,
                                      {p["key"]: p["payee"] for p in payments}, category)
        if not changed:
            return 0
        confirmed_at = _now()
        db.executemany("INSERT OR REPLACE INTO confirmed_payments VALUES (?,?,?,?)", (
            (p["key"], category, json.dumps(payment_evidence(p) | {
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

    def suggest(self, actor, seed, category):
        self.engine.authorize(actor)
        currency, amount = payment_amount(seed["amount"])
        if amount is None:
            return None, 0
        with self.engine.db() as db:
            examples = [seed]
            for row in db.execute("""SELECT payment FROM confirmed_payments
                WHERE category=? AND key<>? ORDER BY confirmed_at DESC LIMIT 20""", (category, seed["key"])):
                payment = json.loads(row["payment"])
                if payment["direction"] == seed["direction"] and payment_amount(payment["amount"])[0] == currency:
                    examples.append(payment)
                    if len(examples) == 5:
                        break
            # Older Discord approvals were payee-wide. Use those as additional
            # examples when there are few individual confirmations.
            seen = {example["key"] for example in examples}
            for row in db.execute("""SELECT payee,candidate FROM reviews
                WHERE state='approved' AND approved_by IS NOT NULL AND category=?
                ORDER BY updated_at DESC LIMIT 20""", (category,)):
                if len(examples) == 5:
                    break
                for payment in json.loads(row["candidate"])["examples"]:
                    if (payment["key"] not in seen and payment["direction"] == seed["direction"]
                            and payment_amount(payment["amount"])[0] == currency):
                        examples.append(dict(payment, payee=row["payee"], confirmed_by_payee=True))
                        seen.add(payment["key"])
                        break

        ranked = []
        for payment in self._unknowns():
            payment_currency, value = payment_amount(payment["amount"])
            if payment_currency != currency or value is None or payment["direction"] != seed["direction"]:
                continue
            nearby = any(near_example(payment, example) for example in examples)
            evidence = dict(payment_evidence(payment), near_confirmed_payment=nearby)
            ranked.append((not nearby, abs(value - amount), payment, evidence))
        ranked.sort(key=lambda item: item[:2])
        shortlist = ranked[:MAX_CANDIDATES]
        if not shortlist:
            return None, 0
        evidence = [dict(payment_evidence(p), confirmation_scope="payee" if p.get("confirmed_by_payee") else "payment")
                    for p in examples]
        scores = decide_payment_matches(self.engine.openai_key, category, evidence,
                                        [entry[3] for entry in shortlist])
        matches = []
        for index, probability in sorted(scores.items(), key=lambda item: item[1], reverse=True):
            if probability >= MIN_PROBABILITY:
                matches.append(dict(shortlist[index][2], probability=probability, included=True))
        if not matches:
            return None, len(shortlist)

        batch_id = uuid.uuid4().hex
        with self.engine.db() as db:
            db.execute("""INSERT INTO payment_match_reviews (id,actor,category,matches,checked)
                VALUES (?,?,?,?,?)""", (batch_id, actor, category, json.dumps(matches[:MAX_MATCHES]), len(shortlist)))
        return self.get(batch_id, actor), len(shortlist)

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
            selected = [match for match in batch["matches"] if match["included"]]
            changed = self._apply(db, selected, batch["category"]) if approve and selected else 0
            db.execute("UPDATE payment_match_reviews SET state='closed' WHERE id=?", (batch_id,))
            return changed, len(selected) - changed
