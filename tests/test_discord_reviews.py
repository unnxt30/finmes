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
                           followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
                           message=SimpleNamespace(edit=AsyncMock(side_effect=AssertionError("Private cards require the interaction edit endpoint"))),
                           data={"values": [category]})


def test_selecting_category_does_not_save_until_confirm_button(engine):
    async def run():
        row = engine.prepare(engine.next()["id"])
        view = ReviewView(engine, row)
        assert view.is_persistent()
        click = interaction()
        await view.on_select(click)
        assert Mappings.load(engine.mappings_path).payees == {}
        new_view = click.edit_original_response.call_args.kwargs["view"]
        assert new_view.version > view.version
        confirm_click = interaction()
        await new_view.on_confirm(confirm_click)
        assert "Saved" in confirm_click.edit_original_response.call_args.kwargs["content"]
        click.message.edit.assert_not_called()
        confirm_click.message.edit.assert_not_called()
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


@pytest.mark.parametrize("action", ["on_refresh", "on_skip"])
def test_private_review_buttons_use_interaction_edit_endpoint(engine, action):
    async def run():
        row = engine.prepare(engine.next()["id"])
        click = interaction()
        await getattr(ReviewView(engine, row), action)(click)
        click.edit_original_response.assert_awaited_once()
        click.message.edit.assert_not_called()
        assert Mappings.load(engine.mappings_path).payees == {}
    asyncio.run(run())


def test_category_modal_without_source_message_sends_private_followup(engine):
    async def run():
        row = engine.prepare(engine.next()["id"])
        click = interaction()
        click.message = None
        await ReviewView(engine, row).choose(click, "expenses:health")
        click.followup.send.assert_awaited_once()
        assert click.followup.send.call_args.kwargs["ephemeral"] is True
        assert Mappings.load(engine.mappings_path).payees == {}
    asyncio.run(run())


def add_unknowns(engine, count=3):
    with (engine.journal_dir / '2026.journal').open('a') as journal:
        for i in range(count):
            journal.write(f'\n2026-10-17 SHOP{i}  ; key:extra{i}.XX973.debit\n'
                          '    expenses:unknown  ₹200.00\n    assets:bank:icici\n')
    engine.scan()


def test_batch_advances_on_confirm_and_dismiss_and_stops_at_limit(engine):
    async def run():
        add_unknowns(engine)
        row = engine.prepare(engine.next()['id'])
        batch_id = engine.start_batch(10, row, 2)
        first = ReviewView(engine, row, batch_id)
        click = interaction()
        await first.on_confirm(click)
        next_view = click.edit_original_response.call_args.kwargs['view']
        assert next_view.review_id != first.review_id
        assert next_view.batch_id == batch_id
        assert engine.batch(batch_id)['position'] == 1
        assert len(Mappings.load(engine.mappings_path).payees) == 1
        dismiss = interaction()
        await next_view.on_skip(dismiss)
        assert 'session complete' in dismiss.edit_original_response.call_args.kwargs['content']
        assert engine.batch_next(batch_id) is None
        assert engine.next() is not None  # Limit leaves the rest untouched.
        assert len(Mappings.load(engine.mappings_path).payees) == 1
    asyncio.run(run())


def test_batch_survives_restart_and_select_does_not_advance(engine):
    async def run():
        add_unknowns(engine)
        row = engine.prepare(engine.next()['id'])
        batch_id = engine.start_batch(10, row)
        assert len(engine.batch(batch_id)['queue']) == 4
        click = interaction()
        await ReviewView(engine, row, batch_id).on_select(click)
        view = click.edit_original_response.call_args.kwargs['view']
        assert view.batch_id == batch_id
        assert engine.batch(batch_id)['position'] == 0
        restarted = ReviewEngine(engine.data_dir, {10})
        assert restarted.batch_next(batch_id)['id'] == row['id']
        bot = FinanceBot(restarted, HermesClient('http://hermes:8642', 'test'), None)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        assert bot.watch_task is None  # No unsolicited reviews or provider calls on startup.
        assert any(getattr(v, 'batch_id', None) == batch_id for v in bot.persistent_views)
        await bot.close()
    asyncio.run(run())


def test_loading_next_failure_does_not_report_confirmation_failed(engine, monkeypatch):
    async def run():
        add_unknowns(engine)
        row = engine.prepare(engine.next()['id'])
        batch_id = engine.start_batch(10, row, 2)
        monkeypatch.setattr(engine, 'prepare', lambda *_: (_ for _ in ()).throw(RuntimeError('provider failed')))
        click = interaction()
        await ReviewView(engine, row, batch_id).on_confirm(click)
        assert 'Saved' in click.edit_original_response.call_args.kwargs['content']
        assert 'previous action succeeded' in click.followup.send.call_args.args[0]
        assert len(Mappings.load(engine.mappings_path).payees) == 1
    asyncio.run(run())


def test_custom_category_becomes_preset_only_after_confirmation(engine):
    async def run():
        row = engine.prepare(engine.next()['id'])
        selected = engine.choose(row['id'], row['version'], 10, 'expenses:investment')
        def values(view):
            return [o.value for item in view.children if hasattr(item, 'options') for o in item.options]
        # A different review should not learn an unsaved selection.
        other = dict(row, category=None)
        assert 'expenses:investment' not in values(ReviewView(engine, other))
        engine.confirm(row['id'], selected['version'], 10)
        assert 'expenses:investment' in values(ReviewView(engine, other))
    asyncio.run(run())


def test_presets_paginate_instead_of_losing_custom_categories(engine):
    async def run():
        with engine.mappings_path.open('a') as mappings:
            for i in range(40):
                mappings.write(f'"merchant{i}" = "expenses:custom{i}"\n')
        row = engine.prepare(engine.next()['id'])
        batch_id = engine.start_batch(10, row, 1)
        view = ReviewView(engine, row, batch_id)
        seen = set()
        for page in range(view.category_pages):
            page_view = ReviewView(engine, row, batch_id, page)
            assert all(len(item.custom_id) <= 100 for item in page_view.children)
            select = next(item for item in page_view.children if hasattr(item, 'options'))
            assert len(select.options) <= 25
            seen.update(option.value for option in select.options)
        assert all(f'expenses:custom{i}' in seen for i in range(40))
        next_button = next(item for item in view.children if 'review:next:' in item.custom_id)
        click = interaction()
        await next_button.callback(click)
        assert click.edit_original_response.call_args.kwargs['view'].category_page == 1
        assert engine.batch(batch_id)['position'] == 0
    asyncio.run(run())


def test_review_command_exposes_bounded_count(engine):
    async def run():
        bot = FinanceBot(engine, HermesClient('http://hermes:8642', 'test'), None)
        param = next(p for p in bot.tree.get_command('review').parameters if p.name == 'count')
        assert not param.required
        assert (param.min_value, param.max_value) == (1, 50)
        await bot.close()
    asyncio.run(run())
