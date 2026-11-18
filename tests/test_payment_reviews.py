import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finmes.bulk_discord import BulkItem
from finmes.bulk_reviews import BulkReviews
from finmes.discord_bot import ReviewView
from finmes.enrichment import Suggestion
from finmes.payment_discord import PaymentMatches
from finmes.reviews import ReviewEngine

COMMUTING = "expenses:transport:commuting"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    journal = tmp_path / "journal"
    journal.mkdir()
    for year, rows in {
        2025: [("driver-a", "AUTO TRAVEL", "355"), ("wrong", "GROCERY", "360")],
        2026: [("example", "DRIVER B", "361"), ("transfer", "DRIVER B", "900"),
               ("driver-c", "LOCAL CAB", "367")],
    }.items():
        (journal / f"{year}.journal").write_text("\n\n".join(
            f"{year}-10-18 {payee}  ; key:{key}, time:18:10, lat:12.12345, lon:78.12345\n"
            f"    expenses:unknown  ₹{amount}.00\n    assets:bank:icici"
            for key, payee, amount in rows) + "\n")
    (tmp_path / "mappings.toml").write_text("[payees]\n")
    monkeypatch.setattr("finmes.reviews.decide_category",
                        lambda *args: Suggestion(COMMUTING, .9, "suggested"))
    engine = ReviewEngine(tmp_path, {10, 20}, openai_key="fake")
    engine.scan()
    return engine


def test_single_confirmation_and_cross_payee_approval(engine, monkeypatch):
    captured = {}

    def decisions(request):
        body = json.loads(request.data)
        evidence = json.loads(body["input"])
        captured.update(evidence)
        scores = {"AUTO TRAVEL": .96, "LOCAL CAB": .92, "GROCERY": .12, "DRIVER B": .33}
        return {"answers": [
            {"name": question["name"], "type": "predicate", "probability": scores[payment["payee"]]}
            for question, payment in zip(body["questions"], evidence["candidate_payments"], strict=True)
        ]}

    monkeypatch.setattr("finmes.enrichment._json_request", decisions)
    review = engine.prepare(engine.next(payee="DRIVER B")["id"])
    before = engine.mappings_path.read_bytes()
    seed, category = engine.payments.confirm(review["id"], review["version"], 10, "example")
    with pytest.raises(ValueError, match="outdated"):
        engine.payments.confirm(review["id"], review["version"], 10, "transfer")
    _, _, remaining = engine.payments.choices(
        review["id"], engine.prepare(review["id"])["version"], 10)
    assert [p["key"] for p in remaining] == ["transfer"]

    batch, checked = engine.payments.suggest(10, seed, category)
    assert checked == 4
    assert captured["confirmed_examples"] == [
        {"payee": "DRIVER B", "amount": "₹361.00", "direction": "debit", "confirmation_scope": "payment"}
    ]
    for payment in captured["candidate_payments"]:
        assert set(payment) == {"payee", "amount", "direction", "near_confirmed_payment"}
    assert [item["key"] for item in batch["matches"]] == ["driver-a", "driver-c"]
    older = engine.journal_dir / "2025.journal"
    assert older.read_text().count("expenses:unknown") == 2

    store = ReviewEngine(engine.data_dir, {10, 20}).payments
    with pytest.raises(PermissionError):
        store.finish(batch["id"], 20, batch["version"], approve=True)
    selected = store.select(batch["id"], 10, batch["version"], {"driver-a"})
    with pytest.raises(ValueError, match="latest"):
        store.finish(batch["id"], 10, batch["version"], approve=True)
    assert store.finish(batch["id"], 10, selected["version"], approve=True) == (1, 0)
    assert older.read_text().count(COMMUTING) == 1
    assert (engine.journal_dir / "2026.journal").read_text().count(COMMUTING) == 1
    assert engine.mappings_path.read_bytes() == before
    assert list(engine.data_dir.glob("backups/*/2025.journal"))


def test_bulk_item_can_open_private_single_payment_review(engine, monkeypatch):
    async def run():
        store = BulkReviews(engine)
        batch = store.create(10)
        index = next(i for i, item in enumerate(batch["items"]) if item["candidate"]["payee"] == "DRIVER B")
        row = engine.get(batch["items"][index]["id"])
        view = BulkItem(store, batch, index, row)
        single = ReviewView(engine, row)
        assert any(getattr(item, "label", "") == "Remember this payee" for item in single.children)
        action = next(item for item in view.children if getattr(item, "label", "") == "Confirm this payment")
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=10), response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock())
        await action.callback(interaction)
        picker = interaction.followup.send.call_args.kwargs["view"]
        assert len(picker.payments) == 2
        assert interaction.followup.send.call_args.kwargs["ephemeral"]
        picker.selected = next(i for i, payment in enumerate(picker.payments) if payment["key"] == "example")

        monkeypatch.setattr("finmes.payment_reviews.decide_payment_matches", lambda *args: {1: .91})
        await picker.confirm(interaction)
        proposal = interaction.followup.send.call_args.kwargs["view"]
        assert isinstance(proposal, PaymentMatches)
        assert proposal.is_persistent()
        with engine.db() as db:
            assert db.execute("SELECT key FROM confirmed_payments").fetchall()[0][0] == "example"

    asyncio.run(run())
