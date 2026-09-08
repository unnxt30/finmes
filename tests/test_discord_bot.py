"""Tests for Discord bot authorization, Hermes HTTP client, and commands."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import finmes.discord_bot as bot
from finmes.discord_bot import (
    HermesClient,
    digest_command,
    extract_question,
    handle_hermes_message,
    parse_allowed_users,
    recent_command,
    search_command,
    spend_command,
)
from finmes.discord_queries import HledgerError


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

    async def defer(self) -> None:
        self.deferred = True


class _FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.ephemeral_messages: list[str] = []

    async def send(self, content: str, *, ephemeral: bool = False) -> None:
        if ephemeral:
            self.ephemeral_messages.append(content)
        else:
            self.messages.append(content)


class _FakeInteraction:
    def __init__(self) -> None:
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


@pytest.fixture
def fake_interaction() -> _FakeInteraction:
    return _FakeInteraction()


@pytest.fixture
def bot_config(tmp_path):
    return bot.BotConfig(
        token="token",
        allowed_users=frozenset({10}),
        home_channel=99,
        api_key="secret",
        hermes_url="http://hermes:8642",
        ledger_file=tmp_path / "main.journal",
    )


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
