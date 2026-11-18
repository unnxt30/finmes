import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from finmes.bulk_reviews import BulkReviews
from finmes.bulk_discord import BulkItem, BulkSummary, summary_embed
from finmes.discord_bot import FinanceBot, HermesClient
from finmes.enrichment import Suggestion
from finmes.journal import Mappings
from finmes.reviews import ReviewEngine, collect_unknowns


@pytest.fixture
def store(tmp_path, monkeypatch):
    (tmp_path / 'journal').mkdir()
    (tmp_path / 'journal/2026.journal').write_text(
        '2026-11-18 HIGH  ; key:a.debit\n    expenses:unknown  ₹200.00\n    assets:bank:test\n\n'
        '2026-11-18 HIGH  ; key:b.debit\n    expenses:unknown  ₹300.00\n    assets:bank:test\n\n'
        '2026-11-17 LOW  ; key:c.debit\n    expenses:unknown  ₹200.00\n    assets:bank:test\n\n'
        '2026-11-16 UNKNOWN  ; key:d.debit\n    expenses:unknown  ₹50.00\n    assets:bank:test\n')
    (tmp_path / 'mappings.toml').write_text('[payees]\n"existing" = "expenses:investment"\n')
    def suggest(candidate, places, key, categories):
        assert 'expenses:investment' in dict(categories)
        if candidate['payee'] == 'UNKNOWN':
            return Suggestion(None, .1, 'Insufficient evidence')
        return Suggestion('expenses:groceries', .95 if candidate['payee'] == 'HIGH' else .4, 'Suggested')
    monkeypatch.setattr('finmes.reviews.decide_category', suggest)
    return BulkReviews(ReviewEngine(tmp_path, {10}, openai_key='fake'))


def interaction(actor=10, values=None):
    return SimpleNamespace(user=SimpleNamespace(id=actor),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True, send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), edit_original_response=AsyncMock(),
        message=object(), data={'values': values or ['0']})


def test_opening_batch_sums_every_transaction_and_never_writes_finance(store):
    before = store.engine.mappings_path.read_bytes()
    journal = (store.engine.journal_dir / '2026.journal').read_bytes()
    batch = store.create(10)
    assert [i['candidate']['payee'] for i in batch['items']] == ['HIGH', 'LOW', 'UNKNOWN']
    assert batch['items'][0]['candidate']['count'] == 2
    assert batch['items'][0]['candidate']['totals'] == {'INR': '500.00'}
    assert [i['included'] for i in batch['items']] == [True, False, False]
    assert store.engine.mappings_path.read_bytes() == before
    assert (store.engine.journal_dir / '2026.journal').read_bytes() == journal


def test_approval_saves_only_visible_included_snapshot_and_cannot_repeat(store):
    batch = store.create(10)
    result = store.approve(batch['id'], 10, batch['version'])
    assert result == {'saved': ['HIGH'], 'failed': [], 'changed': 2, 'pending': 2}
    assert Mappings.load(store.engine.mappings_path).payees['HIGH'] == 'expenses:groceries'
    assert {r['payee'] for r in collect_unknowns(store.engine.journal_dir, store.engine.mappings_path)} == {'LOW', 'UNKNOWN'}
    with pytest.raises(ValueError):
        store.approve(batch['id'], 10, batch['version'])


def test_edit_and_exclude_are_proposals_until_final_approval(store):
    batch = store.create(10)
    low = batch['items'][1]
    row = store.engine.choose(low['id'], low['version'], 10, 'expenses:investment')
    batch = store.update(batch['id'], 10, 1, row, manual=True)
    high = store.engine.get(batch['items'][0]['id'])
    batch = store.update(batch['id'], 10, 0, high, include=False)
    assert 'LOW' not in Mappings.load(store.engine.mappings_path).payees
    result = store.approve(batch['id'], 10, batch['version'])
    assert result['saved'] == ['LOW']
    assert Mappings.load(store.engine.mappings_path).payees['LOW'] == 'expenses:investment'


def test_stale_snapshot_or_unauthorized_actor_cannot_save_anything(store):
    batch = store.create(10)
    with pytest.raises(PermissionError):
        store.approve(batch['id'], 11, batch['version'])
    store.engine.allowed_users = frozenset({10, 11})
    with pytest.raises(PermissionError):
        store.approve(batch['id'], 11, batch['version'])
    item = batch['items'][0]
    store.engine.choose(item['id'], item['version'], 10, 'expenses:health')
    with pytest.raises(ValueError, match='outdated'):
        store.approve(batch['id'], 10, batch['version'])
    assert 'HIGH' not in Mappings.load(store.engine.mappings_path).payees


def test_close_and_restart_preserve_pending_and_snapshot(store):
    batch = store.create(10, 2)
    restarted = BulkReviews(ReviewEngine(store.engine.data_dir, {10}))
    assert restarted.get(batch['id'], 10)['items'] == batch['items']
    restarted.close(batch['id'], 10, batch['version'])
    assert len(collect_unknowns(store.engine.journal_dir, store.engine.mappings_path)) == 3
    with pytest.raises(ValueError):
        store.approve(batch['id'], 10, batch['version'])


def test_unknown_cannot_be_included_without_category(store):
    batch = store.create(10)
    with pytest.raises(ValueError, match='Choose a category'):
        store.update(batch['id'], 10, 2, store.engine.get(batch['items'][2]['id']), include=True)


def test_partial_failure_is_reported_without_hiding_success(store, monkeypatch):
    batch = store.create(10)
    low = store.engine.get(batch['items'][1]['id'])
    batch = store.update(batch['id'], 10, 1, low, include=True)
    original = store.engine.confirm
    def confirm(review_id, *args):
        if review_id == low['id']:
            raise OSError('disk unavailable')
        return original(review_id, *args)
    monkeypatch.setattr(store.engine, 'confirm', confirm)
    result = store.approve(batch['id'], 10, batch['version'])
    assert result['saved'] == ['HIGH'] and result['failed'] == ['LOW']
    assert store.engine.get(low['id'])['state'] == 'pending'


def test_summary_editing_and_return_never_individually_confirm(store):
    async def run():
        batch = store.create(10)
        summary = BulkSummary(store, batch)
        approve = next(c for c in summary.children if ':approve:' in c.custom_id)
        assert approve.label == 'Approve 1 payee mappings'
        click = interaction(values=['1'])
        await summary.edit(click)
        view = click.edit_original_response.call_args.kwargs['view']
        await view.choose(interaction(), 'expenses:investment')
        updated = store.get(batch['id'], 10)
        row = store.engine.get(updated['items'][1]['id'])
        view = BulkItem(store, updated, 1, row)
        back_click = interaction()
        await view.on_confirm(back_click)
        assert isinstance(back_click.edit_original_response.call_args.kwargs['view'], BulkSummary)
        assert 'LOW' not in Mappings.load(store.engine.mappings_path).payees
        await BulkSummary(store, store.get(batch['id'], 10)).approve(interaction())
        assert Mappings.load(store.engine.mappings_path).payees['LOW'] == 'expenses:investment'
    asyncio.run(run())


def test_bulk_views_restore_after_restart_without_notifications(store):
    async def run():
        batch = store.create(10)
        bot = FinanceBot(store.engine, HermesClient('http://hermes:8642', 'test'), None)
        bot.tree.sync = AsyncMock()
        await bot.setup_hook()
        assert bot.watch_task is None
        assert any(isinstance(view, BulkSummary) for view in bot.persistent_views)
        for view in bot.persistent_views:
            for child in view.children:
                assert len(child.custom_id) <= 100
        assert len(summary_embed(batch).fields) == 3
        await bot.close()
    asyncio.run(run())


def test_default_review_command_creates_summary_without_finance_writes(store):
    async def run():
        bot = FinanceBot(store.engine, HermesClient('http://hermes:8642', 'test'), None)
        click = interaction()
        await bot.tree.get_command('review').callback(click, count=2)
        assert isinstance(click.followup.send.call_args.kwargs['view'], BulkSummary)
        assert set(Mappings.load(store.engine.mappings_path).payees) == {'existing'}
        await bot.close()
    asyncio.run(run())


def test_private_summary_remains_active_after_library_ephemeral_timeout(store):
    from finmes.discord_bot import send_private_review
    async def run():
        batch = store.create(10)
        bot = FinanceBot(store.engine, HermesClient('http://hermes:8642', 'test'), None)
        view = BulkSummary(store, batch)
        click = interaction()
        async def ephemeral_send(**kwargs):
            # Simulate discord.py's injected timeout, shortened for the regression.
            kwargs['view'].timeout = .01
            bot._connection.store_view(kwargs['view'], 123)
        click.followup.send.side_effect = ephemeral_send
        await send_private_review(click, embed=summary_embed(batch), view=view)
        await asyncio.sleep(.03)
        assert view.timeout is None
        assert not view.is_finished()
        assert view.is_persistent()
        assert 'HIGH' not in Mappings.load(store.engine.mappings_path).payees
        view.stop()
        await bot.close()
    asyncio.run(run())


def test_edit_acknowledges_before_loading_and_only_once(store, monkeypatch):
    async def run():
        batch = store.create(10)
        view = BulkSummary(store, batch)
        click = interaction()
        done = False
        async def defer():
            nonlocal done
            done = True
        click.response.is_done = lambda: done
        click.response.defer.side_effect = defer
        original = store.get
        def get(*args, **kwargs):
            assert done, 'Database reads must follow the initial acknowledgement'
            return original(*args, **kwargs)
        monkeypatch.setattr(store, 'get', get)
        assert await view.interaction_check(click)
        await view.edit(click)
        click.response.defer.assert_awaited_once()
        assert isinstance(click.edit_original_response.call_args.kwargs['view'], BulkItem)
        assert 'HIGH' not in Mappings.load(store.engine.mappings_path).payees
    asyncio.run(run())
