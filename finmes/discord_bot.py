"""Discord bot service: authorization, Hermes client, and bot factory."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord import app_commands
from discord.ext import commands

from finmes.discord_queries import (
    HledgerError,
    chunk_text,
    recent_transactions,
    search_transactions,
    spending_report,
)

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


def extract_question(
    message: discord.Message, bot_user: discord.ClientUser | None
) -> str | None:
    if message.guild is None:
        text = message.content.strip()
    else:
        if bot_user is None or bot_user not in message.mentions:
            return None
        text = re.sub(rf"<@!?{bot_user.id}>", "", message.content).strip()
    return text or None


async def _send_code_blocks(followup: discord.Webhook, text: str) -> None:
    chunks = chunk_text(text)
    if not chunks:
        await followup.send("```\n(no output)\n```")
        return
    for chunk in chunks:
        await followup.send(f"```\n{chunk}\n```")


async def _run_query_command(
    interaction: discord.Interaction, query_fn
) -> None:
    await interaction.response.defer()
    try:
        result = await asyncio.to_thread(query_fn)
    except (HledgerError, ValueError) as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    await _send_code_blocks(interaction.followup, result)


async def spend_command(
    interaction: discord.Interaction,
    config: BotConfig,
    *,
    period: str = "thismonth",
    category: str | None = None,
) -> None:
    await _run_query_command(
        interaction,
        lambda: spending_report(
            period, category, ledger_file=config.ledger_file
        ),
    )


async def recent_command(
    interaction: discord.Interaction,
    config: BotConfig,
    *,
    limit: int = 10,
) -> None:
    await _run_query_command(
        interaction,
        lambda: recent_transactions(limit, ledger_file=config.ledger_file),
    )


async def search_command(
    interaction: discord.Interaction,
    config: BotConfig,
    *,
    query: str,
) -> None:
    await _run_query_command(
        interaction,
        lambda: search_transactions(query, ledger_file=config.ledger_file),
    )


async def digest_command(
    interaction: discord.Interaction,
    config: BotConfig,
    *,
    period: str = "thisweek",
) -> None:
    await _run_query_command(
        interaction,
        lambda: spending_report(period, None, ledger_file=config.ledger_file),
    )


async def handle_hermes_message(
    message: discord.Message,
    config: BotConfig,
    hermes: HermesClient,
    bot_user: discord.ClientUser | None,
) -> None:
    if message.author.bot:
        return
    if not is_authorized(message.author.id, config):
        return

    question = extract_question(message, bot_user)
    if question is None:
        return

    async with message.channel.typing():
        answer = await hermes.answer_async(question)

    for chunk in chunk_text(answer) or [answer]:
        await message.reply(chunk)


def _register_commands(bot: commands.Bot, config: BotConfig) -> None:
    @bot.tree.command(name="spend", description="Show spending totals for a period")
    @app_commands.describe(
        period="hledger period, e.g. thismonth or thisweek",
        category="Optional expense account, e.g. expenses:food",
    )
    async def spend(
        interaction: discord.Interaction,
        period: str = "thismonth",
        category: str | None = None,
    ) -> None:
        await spend_command(
            interaction, config, period=period, category=category
        )

    @bot.tree.command(name="recent", description="Show recent expense transactions")
    @app_commands.describe(limit="Number of transactions to show (1-25)")
    async def recent(
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 25] = 10,
    ) -> None:
        await recent_command(interaction, config, limit=limit)

    @bot.tree.command(name="search", description="Search transactions by description")
    @app_commands.describe(query="Text to match in transaction descriptions")
    async def search(interaction: discord.Interaction, query: str) -> None:
        await search_command(interaction, config, query=query)

    @bot.tree.command(name="digest", description="Show a spending digest for a period")
    @app_commands.describe(period="hledger period, e.g. thisweek")
    async def digest(
        interaction: discord.Interaction, period: str = "thisweek"
    ) -> None:
        await digest_command(interaction, config, period=period)


def create_bot(config: BotConfig) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)
    bot.config = config
    bot.hermes = HermesClient(config.hermes_url, config.api_key)

    @bot.tree.interaction_check
    async def authorize_interaction(interaction: discord.Interaction) -> bool:
        return is_authorized(interaction.user.id, config)

    _register_commands(bot, config)

    @bot.event
    async def on_message(message: discord.Message) -> None:
        await handle_hermes_message(message, config, bot.hermes, bot.user)

    return bot


def main() -> int:
    config = BotConfig.from_env()
    bot = create_bot(config)
    bot.run(config.token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
