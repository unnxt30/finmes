import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finmes.discord_bot import FinanceBot, HermesClient, ReviewView, review_embed
from finmes.enrichment import Suggestion
from finmes.journal import Mappings
from finmes.reviews import ReviewEngine


@pytest.fixture
def engine(tmp_path, monkeypatch):
    (tmp_path / "journal").mkdir()
    (tmp_path / "journal/2026.journal").write_text(
        "2026-10-18 SHOP  ; key:example.XX973.debit, time:14:30\n"
        "    expenses:unknown  ₹500.00\n    assets:bank:icici\n")
    (tmp_path / "mappings.toml").write_text("[payees]\n")
    monkeypatch.setattr("finmes.reviews.decide_category", lambda *a: Suggestion("expenses:groceries", .85, "Confirm to save."))
    engine = ReviewEngine(tmp_path, {10}, openai_key="fake")
    engine.scan()
    return engine


def interaction(actor=10, category="expenses:health"):
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: True)
    return SimpleNamespace(user=SimpleNamespace(id=actor), response=response,
                           followup=SimpleNamespace(send=AsyncMock()), message=SimpleNamespace(edit=AsyncMock()),
                           data={"values": [category]})


def test_selecting_category_does_not_save_until_confirm_button(engine):
    async def run():
        row = engine.prepare(engine.next()["id"])
        view = ReviewView(engine, row)
        assert view.is_persistent()
        click = interaction()
        await view.on_select(click)
        assert Mappings.load(engine.mappings_path).payees == {}
        new_view = click.message.edit.call_args.kwargs["view"]
        assert new_view.version > view.version
        await new_view.on_confirm(interaction())
        assert Mappings.load(engine.mappings_path).payees == {"SHOP": "expenses:health"}
    asyncio.run(run())


def test_confirmation_callback_rechecks_authorization(engine):
    async def run():
        row = engine.prepare(engine.next()["id"])
        view = ReviewView(engine, row)
        click = interaction(actor=99)
        await view.on_confirm(click)
        assert Mappings.load(engine.mappings_path).payees == {}
        assert "not authorized" in click.followup.send.call_args.args[0]
    asyncio.run(run())


def test_unclassified_card_cannot_confirm_and_embeds_are_bounded(engine, monkeypatch):
    async def run():
        row = engine.prepare(engine.next()["id"])
        row["category"] = None
        view = ReviewView(engine, row)
        confirm = next(x for x in view.children if "review:confirm:" in x.custom_id)
        assert confirm.disabled
        assert len(review_embed(row).title) <= 256
        assert all(len(x.custom_id) <= 100 for x in view.children)
    asyncio.run(run())


def test_hermes_bridge_uses_authenticated_stable_memory_scope(monkeypatch):
    captured = []
    class Response:
        def read(self):
            return json.dumps({"choices": [{"message": {"content": "answer"}}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def request(req, timeout):
        captured.append(req)
        return Response()
    monkeypatch.setattr("finmes.discord_bot.urlopen", request)
    client = HermesClient("http://hermes:8642", "api-secret")
    assert client.answer("latest transaction", "discord-scope") == "answer"
    assert captured[0].get_header("Authorization") == "Bearer api-secret"
    assert captured[0].get_header("X-hermes-session-key") == "discord-scope"


def test_frontend_registers_review_without_network_calls(engine):
    async def run():
        bot = FinanceBot(engine, HermesClient("http://hermes:8642", "test"), None)
        assert bot.tree.get_command("review") is not None
        await bot.close()
    asyncio.run(run())
