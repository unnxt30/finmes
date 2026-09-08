"""Discord bot service: authorization, Hermes client, and bot factory."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord.ext import commands

_REQUIRED_ENV = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_ALLOWED_USERS",
    "DISCORD_HOME_CHANNEL",
    "API_SERVER_KEY",
)


@dataclass(frozen=True, slots=True)
class BotConfig:
    token: str
    allowed_users: frozenset[int]
    home_channel: int
    api_key: str
    hermes_url: str
    ledger_file: Path

    @classmethod
    def from_env(cls) -> BotConfig:
        missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise SystemExit(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        return cls(
            token=os.environ["DISCORD_BOT_TOKEN"],
            allowed_users=frozenset(
                parse_allowed_users(os.environ["DISCORD_ALLOWED_USERS"])
            ),
            home_channel=int(os.environ["DISCORD_HOME_CHANNEL"]),
            api_key=os.environ["API_SERVER_KEY"],
            hermes_url=os.environ.get("HERMES_URL", "http://hermes:8642").rstrip("/"),
            ledger_file=Path(
                os.environ.get("LEDGER_FILE", "/opt/journal/main.journal")
            ),
        )


def parse_allowed_users(value: str) -> set[int]:
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def is_authorized(user_id: int, config: BotConfig) -> bool:
    return user_id in config.allowed_users


class HermesClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def answer(self, question: str) -> str:
        payload = {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": question}],
        }
        req = Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read())
        except HTTPError as exc:
            return f"Hermes request failed: HTTP {exc.code}"
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError) or (
                isinstance(reason, socket.timeout)
            ):
                return (
                    f"Hermes timed out after {int(self.timeout)} seconds. "
                    "Please try again."
                )
            if reason and "timed out" in str(reason).lower():
                return (
                    f"Hermes timed out after {int(self.timeout)} seconds. "
                    "Please try again."
                )
            return f"Hermes request failed: {reason}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "Hermes returned an invalid response."

        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return "Hermes returned an invalid response."

    async def answer_async(self, question: str) -> str:
        return await asyncio.to_thread(self.answer, question)


def create_bot(config: BotConfig) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    bot.config = config
    bot.hermes = HermesClient(config.hermes_url, config.api_key)

    @bot.tree.interaction_check
    async def authorize_interaction(interaction: discord.Interaction) -> bool:
        return is_authorized(interaction.user.id, config)

    @bot.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return
        if not is_authorized(message.author.id, config):
            return
        await bot.process_commands(message)

    return bot


def main() -> int:
    config = BotConfig.from_env()
    bot = create_bot(config)
    bot.run(config.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
