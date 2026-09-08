"""Tests for Discord bot authorization, Hermes HTTP client, and commands."""

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

import finmes.discord_bot as bot
from finmes.discord_bot import (
    HermesClient,
    PayeeReviewView,
    ReviewUnmappedView,
    collect_unknown_payees,
    digest_command,
    extract_question,
    handle_hermes_message,
    next_month_end_digest,
    next_weekly_digest,
    parse_allowed_users,
    post_month_end_digest,
    post_weekly_digest,
    recent_command,
    review_command,
    search_command,
    send_scheduled_digest,
    spend_command,
)
from finmes.discord_queries import HledgerError
from finmes.journal import Mappings


class _Response:
    def __init__(self, capture: dict, req) -> None:
        capture["value"] = req
        self._body = json.dumps(
            {"choices": [{"message": {"content": "You spent ₹500"}}]}
        ).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None


class _FakeResponse:
    def __init__(self) -> None:
        self.deferred = False

    def is_done(self) -> bool:
        return self.deferred

    async def defer(self) -> None:
        self.deferred = True


class _FakeChannel:
    def __init__(self) -> None:
        self.messages: list = []
        self.embeds: list = []
        self.views: list = []

    async def send(
        self,
        content: str | None = None,
        *,
        embed=None,
        view=None,
    ) -> None:
        if content:
            self.messages.append(content)
        if embed is not None:
            self.embeds.append(embed)
        if view is not None:
            self.views.append(view)


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.ephemeral_messages: list[str] = []
        self.embeds: list = []

    async def send(
        self,
        content: str | None = None,
        *,
        embed=None,
        embeds=None,
        ephemeral: bool = False,
        view=None,
    ) -> None:
        if embed is not None:
            self.embeds.append(embed)
        if embeds:
            self.embeds.extend(embeds)
        if content:
            if ephemeral:
                self.ephemeral_messages.append(content)
            else:
                self.messages.append(content)


class _FakeMessage:
    def __init__(self) -> None:
        self.edited: list[tuple] = []

    async def edit(self, *, embed=None, view=None, content=None) -> None:
        self.edited.append((embed, view, content))


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()
        self.message = _FakeMessage()


@pytest.fixture
def fake_interaction() -> _FakeInteraction:
    return _FakeInteraction()


def _config(tmp_path) -> bot.BotConfig:
    journal = tmp_path / "journal"
    journal.mkdir()
    mappings = tmp_path / "mappings.toml"
    mappings.write_text('[payees]\nBlinkit = "expenses:groceries"\n')
    ingest = tmp_path / "ingest.jsonl"
    ingest.write_text("")
    return bot.BotConfig(
        token="token",
        allowed_users=frozenset({10}),
        home_channel=99,
        api_key="secret",
        hermes_url="http://hermes:8642",
        ledger_file=journal / "main.journal",
        mappings_path=mappings,
        ingest_log_path=ingest,
        journal_dir=journal,
    )


@pytest.fixture
def bot_config(tmp_path):
    return _config(tmp_path)


def test_parse_allowed_users_accepts_comma_separated_ids():
    assert parse_allowed_users("10, 20") == {10, 20}


def test_hermes_client_sends_bearer_key(monkeypatch):
    request = {}
    monkeypatch.setattr(bot, "urlopen", lambda req, timeout: _Response(request, req))
    HermesClient("http://hermes:8642", "secret").answer("What did I spend?")
    assert request["value"].get_header("Authorization") == "Bearer secret"


def test_spend_command_defers_then_sends_report(
    fake_interaction, bot_config, monkeypatch
):
    monkeypatch.setattr(
        bot, "spending_report", lambda *args, **kwargs: "₹1,000"
    )
    asyncio.run(
        spend_command(
            fake_interaction, bot_config, period="thismonth", category=None
        )
    )
    assert fake_interaction.response.deferred is True
    assert fake_interaction.followup.messages == ["```\n₹1,000\n```"]


def test_spend_command_returns_hledger_error_ephemerally(
    fake_interaction, bot_config, monkeypatch
):
    def _fail(*args, **kwargs):
        raise HledgerError("ledger failed")

    monkeypatch.setattr(bot, "spending_report", _fail)
    asyncio.run(
        spend_command(
            fake_interaction, bot_config, period="thismonth", category=None
        )
    )
    assert fake_interaction.response.deferred is True
    assert fake_interaction.followup.ephemeral_messages == ["ledger failed"]


def test_recent_command_defers_then_sends_register(
    fake_interaction, bot_config, monkeypatch
):
    monkeypatch.setattr(
        bot, "recent_transactions", lambda *args, **kwargs: "2026-01-01 txn"
    )
    asyncio.run(recent_command(fake_interaction, bot_config, limit=5))
    assert fake_interaction.response.deferred is True
    assert fake_interaction.followup.messages == ["```\n2026-01-01 txn\n```"]


def test_search_command_defers_then_sends_matches(
    fake_interaction, bot_config, monkeypatch
):
    monkeypatch.setattr(
        bot, "search_transactions", lambda *args, **kwargs: "blinkit entry"
    )
    asyncio.run(search_command(fake_interaction, bot_config, query="blinkit"))
    assert fake_interaction.response.deferred is True
    assert fake_interaction.followup.messages == ["```\nblinkit entry\n```"]


def test_digest_command_defers_then_sends_report(
    fake_interaction, bot_config, monkeypatch
):
    captured = []

    def _report(period, category, *, ledger_file):
        captured.append((period, category, ledger_file))
        return "weekly digest"

    monkeypatch.setattr(bot, "spending_report", _report)
    asyncio.run(digest_command(fake_interaction, bot_config, period="thisweek"))
    assert captured == [("thisweek", None, bot_config.ledger_file)]
    assert fake_interaction.response.deferred is True
    assert fake_interaction.followup.messages == ["```\nweekly digest\n```"]


def test_extract_question_uses_full_dm_content():
    message = SimpleNamespace(
        guild=None,
        content="  What did I spend on food?  ",
        mentions=[],
    )
    assert extract_question(message, SimpleNamespace(id=1)) == "What did I spend on food?"


def test_extract_question_strips_guild_mention():
    bot_user = SimpleNamespace(id=42)
    message = SimpleNamespace(
        guild=object(),
        content="<@!42> how much on groceries?",
        mentions=[bot_user],
    )
    assert extract_question(message, bot_user) == "how much on groceries?"


def test_extract_question_ignores_empty_guild_mention():
    bot_user = SimpleNamespace(id=42)
    message = SimpleNamespace(
        guild=object(),
        content="<@42>",
        mentions=[bot_user],
    )
    assert extract_question(message, bot_user) is None


def _typing_context():
    typing_cm = AsyncMock()
    typing_cm.__aenter__ = AsyncMock(return_value=None)
    typing_cm.__aexit__ = AsyncMock(return_value=None)
    return typing_cm


def test_direct_message_routes_to_hermes():
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=10),
        guild=None,
        content="What did I spend?",
        mentions=[],
        reply=AsyncMock(),
        channel=SimpleNamespace(typing=lambda: _typing_context()),
    )

    hermes = SimpleNamespace(answer_async=AsyncMock(return_value="You spent ₹500"))
    config = SimpleNamespace(allowed_users=frozenset({10}))

    asyncio.run(
        handle_hermes_message(message, config, hermes, SimpleNamespace(id=1))
    )

    hermes.answer_async.assert_awaited_once_with("What did I spend?")
    message.reply.assert_awaited_once_with("You spent ₹500")


def test_mention_routes_to_hermes():
    bot_user = SimpleNamespace(id=42)
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=10),
        guild=object(),
        content="<@42> top payees this week",
        mentions=[bot_user],
        reply=AsyncMock(),
        channel=SimpleNamespace(typing=lambda: _typing_context()),
    )

    hermes = SimpleNamespace(answer_async=AsyncMock(return_value="Blinkit leads"))
    config = SimpleNamespace(allowed_users=frozenset({10}))

    asyncio.run(handle_hermes_message(message, config, hermes, bot_user))

    hermes.answer_async.assert_awaited_once_with("top payees this week")
    message.reply.assert_awaited_once_with("Blinkit leads")


SMS_ZEPTO = (
    "ICICI Bank Acct XX973 debited for Rs 165.00 on 29-Jun-26; "
    "ZEPTOMARKETPLAC credited. UPI:203007126186."
)


def _write_ingest(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_mapping_callback_updates_payees_and_rebuilds(
    fake_interaction, tmp_path, monkeypatch
):
    called = []
    monkeypatch.setattr(
        bot, "rebuild_journal", lambda *args: called.append(args) or SimpleNamespace()
    )
    config = _config(tmp_path)
    view = PayeeReviewView(payee="ZEPTOMARKETPLAC", config=config)
    asyncio.run(view.map_to(fake_interaction, "expenses:food:groceries"))
    assert (
        Mappings.load(tmp_path / "mappings.toml").payees["ZEPTOMARKETPLAC"]
        == "expenses:food:groceries"
    )
    assert len(called) == 1


def test_review_collects_unknown_debit_payees(tmp_path):
    config = _config(tmp_path)
    _write_ingest(
        config.ingest_log_path,
        [
            {
                "at": "2026-06-29T10:00:00",
                "payload": {"sender": "ICICIT-S(x)", "text": SMS_ZEPTO},
            },
            {
                "at": "2026-06-29T11:00:00",
                "payload": {"sender": "ICICIT-S(x)", "text": SMS_ZEPTO},
            },
        ],
    )
    candidates = collect_unknown_payees(config)
    assert len(candidates) == 1
    assert candidates[0].payee == "ZEPTOMARKETPLAC"
    assert candidates[0].amount == Decimal("165.00")
    assert candidates[0].date == date(2026, 6, 29)


def test_review_command_defers_and_sends_embeds(
    fake_interaction, tmp_path, monkeypatch
):
    config = _config(tmp_path)
    candidate = SimpleNamespace(
        payee="ZEPTOMARKETPLAC",
        date=date(2026, 6, 29),
        amount=Decimal("165.00"),
        ref="203007126186",
        location=None,
        raw=SMS_ZEPTO,
    )
    monkeypatch.setattr(bot, "collect_unknown_payees", lambda cfg: [candidate])
    asyncio.run(review_command(fake_interaction, config))
    assert fake_interaction.response.deferred is True
    assert len(fake_interaction.followup.embeds) == 1
    assert fake_interaction.followup.embeds[0].title == "ZEPTOMARKETPLAC"


def test_next_weekly_digest_uses_monday_at_0800_ist():
    now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert next_weekly_digest(now) == datetime(
        2026, 9, 14, 8, tzinfo=ZoneInfo("Asia/Kolkata")
    )


def test_next_month_end_digest_uses_first_at_0800_ist():
    now = datetime(2026, 9, 8, 9, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert next_month_end_digest(now) == datetime(
        2026, 10, 1, 8, tzinfo=ZoneInfo("Asia/Kolkata")
    )


def test_send_scheduled_digest_posts_sections_and_review_button(
    bot_config, monkeypatch
):
    channel = _FakeChannel()
    fake_bot = SimpleNamespace(get_channel=lambda _: channel)
    monkeypatch.setattr(
        bot,
        "build_digest_sections",
        lambda cfg, period: ("cat report", "payee report", 2),
    )

    asyncio.run(
        send_scheduled_digest(
            fake_bot,
            bot_config,
            title="Weekly spending digest",
            period="lastweek",
        )
    )

    assert channel.embeds[0].title == "Weekly spending digest"
    assert channel.embeds[0].fields[0].value == "2"
    assert len(channel.views) == 1
    assert isinstance(channel.views[0], ReviewUnmappedView)
    assert any("cat report" in msg for msg in channel.messages)
    assert any("payee report" in msg for msg in channel.messages)


def test_send_scheduled_digest_omits_review_button_without_unknowns(
    bot_config, monkeypatch
):
    channel = _FakeChannel()
    fake_bot = SimpleNamespace(get_channel=lambda _: channel)
    monkeypatch.setattr(
        bot,
        "build_digest_sections",
        lambda cfg, period: ("cat report", "payee report", 0),
    )

    asyncio.run(
        send_scheduled_digest(
            fake_bot,
            bot_config,
            title="Weekly spending digest",
            period="lastweek",
        )
    )

    assert channel.views == []


def test_post_weekly_digest_uses_lastweek(bot_config, monkeypatch):
    captured = []

    async def _send(fake_bot, cfg, *, title, period):
        captured.append((title, period))

    monkeypatch.setattr(bot, "send_scheduled_digest", _send)
    asyncio.run(post_weekly_digest(SimpleNamespace(), bot_config))
    assert captured == [("Weekly spending digest", "lastweek")]


def test_post_month_end_digest_uses_lastmonth(bot_config, monkeypatch):
    captured = []

    async def _send(fake_bot, cfg, *, title, period):
        captured.append((title, period))

    monkeypatch.setattr(bot, "send_scheduled_digest", _send)
    asyncio.run(post_month_end_digest(SimpleNamespace(), bot_config))
    assert captured == [("Month-end spending digest", "lastmonth")]


def test_hermes_message_ignores_unauthorized_user():
    message = SimpleNamespace(
        author=SimpleNamespace(bot=False, id=99),
        guild=None,
        content="What did I spend?",
        mentions=[],
        reply=AsyncMock(),
    )
    hermes = SimpleNamespace(answer_async=AsyncMock())
    config = SimpleNamespace(allowed_users=frozenset({10}))

    asyncio.run(
        handle_hermes_message(message, config, hermes, SimpleNamespace(id=1))
    )

    hermes.answer_async.assert_not_called()
    message.reply.assert_not_called()
