import json
import stat
from datetime import date, datetime

import pytest

from finmes.categories import confirm_payee_category
from finmes.enrichment import EnrichmentError, Suggestion
from finmes.journal import Mappings, existing_keys, ingest
from finmes.reviews import ReviewEngine, collect_unknowns


def add(data, payee="UNKNOWN SHOP", ref="123456789001", *, location=None, day="2026-10-18"):
    raw = (f"ICICI Bank Acct XX973 debited for Rs 500.00 on {datetime.fromisoformat(day):%d-%b-%y}; "
           f"{payee} credited. UPI:{ref}.")
    ingest([("ios", raw, date.fromisoformat(day), location, datetime.fromisoformat(day + "T14:30:00+05:30"))],
           data / "journal", Mappings.load(data / "mappings.toml"))


@pytest.fixture
def data(tmp_path):
    (tmp_path / "journal").mkdir()
    (tmp_path / "mappings.toml").write_text('[accounts]\nXX973 = "assets:bank:icici"\n\n[payees]\n')
    add(tmp_path, "HISTORICAL SHOP", "123456789000", day="2025-01-01")
    add(tmp_path)
    return tmp_path


@pytest.fixture
def engine(data, monkeypatch):
    monkeypatch.setattr("finmes.reviews.decide_category", lambda *args: Suggestion("expenses:groceries", .84, "Suggested; confirm to save."))
    return ReviewEngine(data, {10}, openai_key="fake", mappls_key="fake")


def finance_snapshot(data):
    return {p.relative_to(data): p.read_bytes() for p in [data / "mappings.toml", *(data / "journal").glob("*.journal")]}


def test_no_finance_writes_until_authorized_confirm(engine, data):
    before = finance_snapshot(data)
    keys = existing_keys(data / "journal")
    engine.scan()
    row = engine.prepare(engine.next()["id"])
    assert row["payee"] == "UNKNOWN SHOP"  # newest first, not oldest insert
    assert finance_snapshot(data) == before
    row = engine.choose(row["id"], row["version"], 10, "expenses:shopping")
    assert finance_snapshot(data) == before
    changed = engine.confirm(row["id"], row["version"], 10)
    assert changed == 1
    assert engine.get(row["id"])["approved_by"] == 10
    assert Mappings.load(data / "mappings.toml").payees["UNKNOWN SHOP"] == "expenses:shopping"
    assert (data / "journal/2025.journal").read_bytes() == before[next(p for p in before if str(p) == "journal/2025.journal")]
    assert existing_keys(data / "journal") == keys
    assert list((data / "backups").glob("*/2026.journal"))
    assert list((data / "backups").glob("*/mappings.toml"))


def test_unauthorized_and_stale_cards_cannot_save(engine, data):
    engine.scan()
    old = engine.prepare(engine.next()["id"])
    before = finance_snapshot(data)
    with pytest.raises(PermissionError):
        engine.confirm(old["id"], old["version"], 99)
    new = engine.choose(old["id"], old["version"], 10, "expenses:health")
    with pytest.raises(ValueError, match="outdated"):
        engine.confirm(old["id"], old["version"], 10)
    assert finance_snapshot(data) == before
    engine.confirm(new["id"], new["version"], 10)
    with pytest.raises(ValueError, match="closed"):
        engine.confirm(new["id"], new["version"], 10)


def test_refusal_and_provider_failure_require_manual_choice(engine, data, monkeypatch):
    monkeypatch.setattr("finmes.reviews.decide_category", lambda *args: Suggestion(None, None, "Refused"))
    engine.scan()
    row = engine.prepare(engine.next()["id"])
    before = finance_snapshot(data)
    with pytest.raises(ValueError, match="Select"):
        engine.confirm(row["id"], row["version"], 10)
    assert finance_snapshot(data) == before
    def fail(*args):
        raise EnrichmentError("HTTP 429")
    monkeypatch.setattr("finmes.reviews.decide_category", fail)
    row = engine.refresh(row["id"], row["version"], 10)
    assert row["category"] is None and "HTTP 429" in row["note"]
    row = engine.choose(row["id"], row["version"], 10, "expenses:health")
    assert engine.confirm(row["id"], row["version"], 10) == 1


def test_new_transactions_notify_but_existing_backlog_does_not(engine, data):
    engine.scan()
    assert engine.next(automatic=True) is None
    add(data, "NEW SHOP", "123456789002")
    engine.scan()
    row = engine.next(automatic=True)
    assert row["payee"] == "NEW SHOP"
    engine.mark_notified(row["id"])
    engine.scan()
    assert engine.next(automatic=True) is None
    add(data, "UNKNOWN SHOP", "123456789003")
    engine.scan()
    assert engine.next(automatic=True)["payee"] == "UNKNOWN SHOP"


def test_pending_reviews_survive_restart_without_extra_api_calls(engine, data, monkeypatch):
    engine.scan()
    row = engine.prepare(engine.next()["id"])
    reloaded = ReviewEngine(data, {10})
    monkeypatch.setattr("finmes.reviews.decide_category", lambda *args: pytest.fail("duplicate API call"))
    assert reloaded.prepare(row["id"])["category"] == "expenses:groceries"
    assert reloaded.pending()[0]["id"] == row["id"]
    reloaded.skip(row["id"], row["version"], 10)
    assert reloaded.get(row["id"])["state"] == "skipped"
    assert reloaded.reopen(row["payee"], 10)["version"] > row["version"]


def test_existing_explicit_mapping_wins_over_stale_suggestion(engine, data):
    engine.scan()
    row = engine.prepare(engine.next()["id"])
    confirm_payee_category(data / "mappings.toml", data / "journal", row["payee"], "expenses:health")
    with pytest.raises(ValueError, match="different mapping"):
        engine.confirm(row["id"], row["version"], 10)
    assert Mappings.load(data / "mappings.toml").payees[row["payee"]] == "expenses:health"


def test_mappls_context_is_cached_and_optional(engine, data, monkeypatch):
    calls = []
    def places(location, key):
        calls.append(location)
        return [{"name": "Nearby grocery", "address": "Street", "distance_m": 50}]
    monkeypatch.setattr("finmes.reviews.nearby_places", places)
    add(data, "LOCATED ONE", "123456789004", location={"lat": "28.6", "lon": "77.2"})
    add(data, "LOCATED TWO", "123456789005", location={"lat": "28.6", "lon": "77.2"})
    engine.scan()
    for name in ("LOCATED ONE", "LOCATED TWO"):
        row = engine.prepare(engine.next(payee=name)["id"])
        assert row["places"][0]["name"] == "Nearby grocery"
    assert len(calls) == 1
    assert "No GPS" in engine.prepare(engine.next(payee="UNKNOWN SHOP")["id"])["note"]


def test_collects_history_without_raw_log_and_preserves_permissions(engine, data):
    assert not (data / "ingest.jsonl").exists()
    assert len(collect_unknowns(data / "journal", data / "mappings.toml")) == 2
    path = data / "journal/2026.journal"
    path.chmod(0o640)
    before = path.stat()
    engine.scan()
    row = engine.prepare(engine.next()["id"])
    engine.confirm(row["id"], row["version"], 10)
    after = path.stat()
    assert (stat.S_IMODE(after.st_mode), after.st_uid, after.st_gid) == (0o640, before.st_uid, before.st_gid)


def test_retry_after_mapping_saved_but_history_update_failed(engine, data, monkeypatch):
    import finmes.categories as categories
    engine.scan()
    row = engine.prepare(engine.next()["id"])
    original = categories._recategorize_locked
    monkeypatch.setattr(categories, "_recategorize_locked", lambda *a, **k: (_ for _ in ()).throw(OSError("disk problem")))
    with pytest.raises(OSError):
        engine.confirm(row["id"], row["version"], 10)
    assert engine.get(row["id"])["state"] == "pending"
    monkeypatch.setattr(categories, "_recategorize_locked", original)
    assert engine.confirm(row["id"], row["version"], 10) == 1
