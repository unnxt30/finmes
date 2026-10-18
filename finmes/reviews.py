"""Durable review queue; only an authorized confirmation writes categories."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from finmes.categories import ACCOUNT_RE, KEY_RE, TIME_RE, _entries, _unknown, confirm_payee_category
from finmes.enrichment import EnrichmentError, coordinates, decide_category, nearby_places
from finmes.journal import Mappings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def collect_unknowns(journal_dir: Path, mappings_path: Path) -> list[dict]:
    """Read actual unknown postings, including history absent from the raw log."""
    mappings = Mappings.load(mappings_path)
    groups = {}
    for _, lines, header in _entries(journal_dir):
        unknown = _unknown(lines)
        key = KEY_RE.search(header[3])
        payee = header[2]
        if not unknown or not key or payee in mappings.payees:
            continue
        row = groups.setdefault(payee, {"payee": payee, "keys": [], "examples": [], "count": 0})
        amount = next((m[1].strip() for line in lines[1:]
                       if (m := re.match(r"^\s+\S+\s{2,}(.+)$", line))), "")
        example = {"date": header[1], "amount": amount,
                   "direction": unknown[2], "key": key[1]}
        if stamp := TIME_RE.search(header[3]):
            example["time"] = stamp[1]
        lat = re.search(r"\blat:([^\s,]+)", header[3])
        lon = re.search(r"\blon:([^\s,]+)", header[3])
        if lat and lon and coordinates({"lat": lat[1], "lon": lon[1]}):
            example["location"] = {"lat": lat[1], "lon": lon[1]}
        row["keys"].append(key[1])
        row["examples"].append(example)
        row["count"] += 1
    for row in groups.values():
        row["examples"].sort(key=lambda e: (e["date"], e.get("time", "")), reverse=True)
        # Keep a located example even if the most recent payments were recapped.
        located = next((e for e in row["examples"] if e.get("location")), None)
        row["examples"] = row["examples"][:3]
        if located and not any(e.get("location") for e in row["examples"]):
            row["examples"][-1] = located
    return sorted(groups.values(), key=lambda r: (r["examples"][0]["date"], r["payee"]), reverse=True)


class ReviewEngine:
    def __init__(self, data_dir: Path, allowed_users: set[int], *,
                 openai_key: str = "", mappls_key: str = ""):
        self.data_dir = Path(data_dir)
        self.journal_dir = self.data_dir / "journal"
        self.mappings_path = self.data_dir / "mappings.toml"
        self.db_path = self.data_dir / "review-state.sqlite3"
        self.allowed_users = frozenset(allowed_users)
        self.openai_key, self.mappls_key = openai_key, mappls_key
        self.prepare_lock = threading.Lock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS seen_keys (key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY, payee TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL DEFAULT 'waiting', version INTEGER NOT NULL DEFAULT 1,
                    candidate TEXT NOT NULL, category TEXT, confidence REAL,
                    places TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT '',
                    auto_notify INTEGER NOT NULL DEFAULT 0, notified_at TEXT,
                    approved_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS places_cache (
                    point TEXT PRIMARY KEY, places TEXT NOT NULL, expires_at TEXT NOT NULL
                );
            """)
        self.db_path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def authorize(self, actor: int):
        if actor not in self.allowed_users:
            raise PermissionError("You are not authorized to review categories.")

    def scan(self) -> int:
        candidates = collect_unknowns(self.journal_dir, self.mappings_path)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            initialized = db.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone()
            for candidate in candidates:
                fresh = False
                for key in candidate["keys"]:
                    fresh |= db.execute("INSERT OR IGNORE INTO seen_keys VALUES (?)", (key,)).rowcount > 0
                candidate.pop("keys")
                now = _now()
                db.execute("""INSERT OR IGNORE INTO reviews
                    (id,payee,candidate,created_at,updated_at) VALUES (?,?,?,?,?)""",
                    (uuid.uuid4().hex, candidate["payee"], json.dumps(candidate), now, now))
                encoded = json.dumps(candidate)
                db.execute("UPDATE reviews SET candidate=?,updated_at=? WHERE payee=? AND state='waiting' AND candidate<>?",
                           (encoded, now, candidate["payee"], encoded))
                if initialized and fresh:
                    db.execute("""UPDATE reviews SET auto_notify=1 WHERE payee=?
                        AND state IN ('waiting','pending') AND notified_at IS NULL""", (candidate["payee"],))
            db.execute("INSERT OR IGNORE INTO settings VALUES ('initialized','1')")
        return len(candidates)

    @staticmethod
    def _decode(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["candidate"] = json.loads(result["candidate"])
        result["places"] = json.loads(result["places"])
        return result

    def get(self, review_id: str) -> dict:
        with self.db() as db:
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
        if row is None:
            raise ValueError("Review no longer exists; run /review again.")
        return self._decode(row)

    def next(self, *, automatic: bool = False, payee: str | None = None) -> dict | None:
        clauses, values = ["state IN ('waiting','pending')"], []
        if automatic:
            clauses += ["auto_notify=1", "notified_at IS NULL"]
        if payee:
            clauses.append("payee=?")
            values.append(payee)
        with self.db() as db:
            row = db.execute("SELECT * FROM reviews WHERE " + " AND ".join(clauses)
                             + " ORDER BY json_extract(candidate, '$.examples[0].date') DESC, updated_at DESC, payee LIMIT 1", values).fetchone()
        return self._decode(row)

    def pending(self) -> list[dict]:
        with self.db() as db:
            return [self._decode(r) for r in db.execute("SELECT * FROM reviews WHERE state='pending'")]

    def reopen(self, payee: str, actor: int) -> dict | None:
        self.authorize(actor)
        with self.db() as db:
            db.execute("UPDATE reviews SET state='waiting',version=version+1,notified_at=NULL,auto_notify=0 "
                       "WHERE payee=? AND state='skipped'", (payee,))
        return self.next(payee=payee)

    def _places(self, location: dict) -> list[dict]:
        point = coordinates(location)
        cache_key = f"{point[0]:.4f},{point[1]:.4f}"
        with self.db() as db:
            row = db.execute("SELECT places FROM places_cache WHERE point=? AND expires_at>?",
                             (cache_key, _now())).fetchone()
        if row:
            return json.loads(row[0])
        places = nearby_places(location, self.mappls_key)
        with self.db() as db:
            expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            db.execute("INSERT OR REPLACE INTO places_cache VALUES (?,?,?)",
                       (cache_key, json.dumps(places), expiry))
        return places

    def prepare(self, review_id: str) -> dict:
        with self.prepare_lock:
            row = self.get(review_id)
            if row["state"] != "waiting":
                return row
            places, notes, category, confidence = [], [], None, None
            location = next((e["location"] for e in row["candidate"]["examples"] if e.get("location")), None)
            if location:
                try:
                    places = self._places(location)
                    notes.append(f"Mappls: {len(places)} nearby candidates; merchant identity is unverified.")
                except EnrichmentError as exc:
                    notes.append(f"Mappls unavailable: {exc}.")
            else:
                notes.append("No GPS for these examples; using payee and transaction context.")
            try:
                suggestion = decide_category(row["candidate"], places, self.openai_key)
                category, confidence = suggestion.category, suggestion.confidence
                notes.append(suggestion.note)
            except EnrichmentError as exc:
                notes.append(f"Decisions unavailable: {exc}.")
            with self.db() as db:
                db.execute("""UPDATE reviews SET state='pending',category=?,confidence=?,places=?,note=?
                    WHERE id=? AND version=? AND state='waiting'""",
                    (category, confidence, json.dumps(places), " ".join(notes), review_id, row["version"]))
            return self.get(review_id)

    @staticmethod
    def _check(row, version):
        if row is None or row["state"] not in {"waiting", "pending"}:
            raise ValueError("This review is already closed; run /review for the next payee.")
        if row["version"] != version:
            raise ValueError("This card is outdated; run /review to see the current choice.")

    def choose(self, review_id: str, version: int, actor: int, category: str) -> dict:
        self.authorize(actor)
        if not ACCOUNT_RE.fullmatch(category):
            raise ValueError("Use a category such as expenses:groceries or assets:transfers.")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            self._check(row, version)
            db.execute("""UPDATE reviews SET category=?,confidence=NULL,version=version+1,
                state='pending',note='Category selected by you; confirm to save.' WHERE id=?""", (category, review_id))
        return self.get(review_id)

    def refresh(self, review_id: str, version: int, actor: int) -> dict:
        self.authorize(actor)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            self._check(row, version)
            db.execute("UPDATE reviews SET state='waiting',version=version+1 WHERE id=?", (review_id,))
        return self.prepare(review_id)

    def confirm(self, review_id: str, version: int, actor: int) -> int:
        self.authorize(actor)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            self._check(row, version)
            if row["state"] != "pending" or not row["category"]:
                raise ValueError("Select a category before confirming.")
            changed = confirm_payee_category(self.mappings_path, self.journal_dir, row["payee"], row["category"])
            db.execute("UPDATE reviews SET state='approved',approved_by=?,updated_at=? WHERE id=?",
                       (actor, _now(), review_id))
        return changed

    def skip(self, review_id: str, version: int, actor: int):
        self.authorize(actor)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            self._check(row, version)
            db.execute("UPDATE reviews SET state='skipped',updated_at=? WHERE id=?", (_now(), review_id))

    def mark_notified(self, review_id: str):
        with self.db() as db:
            db.execute("UPDATE reviews SET notified_at=? WHERE id=?", (_now(), review_id))
