"""Tests for Discord bot authorization and Hermes HTTP client."""

import json

import finmes.discord_bot as bot
from finmes.discord_bot import HermesClient, parse_allowed_users


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


def test_parse_allowed_users_accepts_comma_separated_ids():
    assert parse_allowed_users("10, 20") == {10, 20}


def test_hermes_client_sends_bearer_key(monkeypatch):
    request = {}
    monkeypatch.setattr(bot, "urlopen", lambda req, timeout: _Response(request, req))
    HermesClient("http://hermes:8642", "secret").answer("What did I spend?")
    assert request["value"].get_header("Authorization") == "Bearer secret"
