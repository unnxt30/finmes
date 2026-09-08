"""Discord bot service: authorization, Hermes client, and bot factory."""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord import app_commands
from discord.ext import commands, tasks

from finmes.discord_queries import (
    HledgerError,
    chunk_text,
    recent_transactions,
    run_hledger,
    search_transactions,
    spending_report,
)
from finmes.journal import (
    Mappings,
    read_ingest_log,
    rebuild_journal,
    synthesize_payee,
    upsert_mapping,
)
from finmes.parser import parse

_REQUIRED_ENV = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_ALLOWED_USERS",
    "DISCORD_HOME_CHANNEL",
    "API_SERVER_KEY",
)

REVIEW_CATEGORIES = (
    ("expenses:food:groceries", "Groceries"),
    ("expenses:food:chai", "Chai"),
    ("expenses:transport:metro", "Metro"),
    ("expenses:shopping", "Shopping"),
)

NEARBY_BIN = "/usr/local/bin/nearby"
IST = ZoneInfo("Asia/Kolkata")
_mapping_lock = asyncio.Lock()


@dataclass(frozen=True, slots=True)
class BotConfig:
    token: str
    allowed_users: frozenset[int]
    home_channel: int
    api_key: str
    hermes_url: str
    ledger_file: Path
    mappings_path: Path
    ingest_log_path: Path
    journal_dir: Path

    @classmethod
    def from_env(cls) -> BotConfig:
        missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise SystemExit(
                f"Missing required environment variables: {', '.join(missing)}"
            )

        ledger_file = Path(
            os.environ.get("LEDGER_FILE", "/opt/journal/main.journal")
        )
        repo = Path(os.environ.get("FINMES_REPO", "/app"))
        return cls(
            token=os.environ["DISCORD_BOT_TOKEN"],
            allowed_users=frozenset(
                parse_allowed_users(os.environ["DISCORD_ALLOWED_USERS"])
            ),
            home_channel=int(os.environ["DISCORD_HOME_CHANNEL"]),
            api_key=os.environ["API_SERVER_KEY"],
            hermes_url=os.environ.get("HERMES_URL", "http://hermes:8642").rstrip("/"),
            ledger_file=ledger_file,
            journal_dir=ledger_file.parent,
            mappings_path=Path(
                os.environ.get("FINMES_MAPPINGS", repo / "data/mappings.toml")
            ),
            ingest_log_path=Path(
                os.environ.get("FINMES_INGEST_LOG", repo / "data/ingest.jsonl")
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


@dataclass(frozen=True, slots=True)
class UnknownPayee:
    payee: str
    date: date
    amount: Decimal
    ref: str | None
    location: dict | None
    raw: str


def collect_unknown_payees(config: BotConfig) -> list[UnknownPayee]:
    mappings = Mappings.load(config.mappings_path)
    candidates: dict[str, UnknownPayee] = {}
    for sender, body, received, location in read_ingest_log(config.ingest_log_path):
        txn = parse(sender, body, received=received)
        if txn is None or txn.direction != "debit":
            continue
        payee = txn.counterparty or synthesize_payee(txn)
        if payee in mappings.payees:
            continue
        candidates[payee] = UnknownPayee(
            payee=payee,
            date=txn.date,
            amount=txn.amount,
            ref=txn.ref,
            location=location,
            raw=txn.raw,
        )
    return list(candidates.values())


def _fmt_inr(amount: Decimal) -> str:
    return f"₹{amount.quantize(Decimal('0.01'))}"


def _has_coordinates(location: dict | None) -> bool:
    if not location:
        return False
    try:
        float(location["lat"])
        float(location["lon"])
    except (KeyError, TypeError, ValueError):
        return False
    return True


def build_review_embed(candidate: UnknownPayee) -> discord.Embed:
    embed = discord.Embed(
        title=candidate.payee,
        description=f"**{_fmt_inr(candidate.amount)}** on {candidate.date.isoformat()}",
        color=discord.Color.orange(),
    )
    if candidate.ref:
        embed.add_field(name="UPI ref", value=candidate.ref, inline=True)
    if _has_coordinates(candidate.location):
        lat = round(float(candidate.location["lat"]), 5)
        lon = round(float(candidate.location["lon"]), 5)
        embed.add_field(name="Location", value=f"{lat}, {lon}", inline=True)
    return embed


def _merchant_name(line: str) -> str:
    return line.split(" — ", 1)[0].strip()


class CustomMappingModal(discord.ui.Modal, title="Custom mapping"):
    account = discord.ui.TextInput(
        label="Account",
        placeholder="expenses:health:pharmacy",
        required=True,
    )
    match_pattern = discord.ui.TextInput(
        label="Match pattern",
        required=True,
    )
    marker_type = discord.ui.TextInput(
        label="Marker type",
        placeholder="payee or raw_text",
        default="payee",
        required=True,
    )

    def __init__(self, view: PayeeReviewView) -> None:
        super().__init__()
        self._view = view
        self.match_pattern.default = view._pending_key or view.payee

    async def on_submit(self, interaction: discord.Interaction) -> None:
        table = "markers" if self.marker_type.value.strip() == "raw_text" else "payees"
        await self._view.map_to(
            interaction,
            self.account.value.strip(),
            table=table,
            key=self.match_pattern.value.strip(),
        )


class NearbySuggestionsView(discord.ui.View):
    def __init__(self, parent: PayeeReviewView, suggestions: list[str]) -> None:
        super().__init__(timeout=120)
        self._parent = parent
        for idx, line in enumerate(suggestions[:10]):
            name = _merchant_name(line)
            self.add_item(
                discord.ui.Button(
                    label=name[:80],
                    custom_id=f"nearby:{idx}",
                    style=discord.ButtonStyle.secondary,
                )
            )
            self.children[-1].callback = self._make_callback(name, line)

    def _make_callback(self, name: str, line: str):
        async def callback(interaction: discord.Interaction) -> None:
            self._parent._pending_key = name
            await interaction.response.send_message(
                f"Selected **{name}**. Now pick a category on the review card.",
                ephemeral=True,
            )

        return callback


class PayeeReviewView(discord.ui.View):
    def __init__(
        self,
        payee: str,
        config: BotConfig,
        *,
        location: dict | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.payee = payee
        self.config = config
        self.location = location
        self._pending_key: str | None = None

        options = [
            discord.SelectOption(label=label, value=account)
            for account, label in REVIEW_CATEGORIES
        ]
        category_select = discord.ui.Select(
            placeholder="Pick a category",
            options=options,
            custom_id=f"review:category:{payee}",
        )
        category_select.callback = self._on_category_select
        self.add_item(category_select)

        custom_btn = discord.ui.Button(
            label="Custom mapping",
            style=discord.ButtonStyle.primary,
            custom_id=f"review:custom:{payee}",
        )
        custom_btn.callback = self._on_custom_mapping
        self.add_item(custom_btn)

        if _has_coordinates(location):
            nearby_btn = discord.ui.Button(
                label="Nearby",
                style=discord.ButtonStyle.secondary,
                custom_id=f"review:nearby:{payee}",
            )
            nearby_btn.callback = self._on_nearby
            self.add_item(nearby_btn)

    async def _on_category_select(self, interaction: discord.Interaction) -> None:
        account = interaction.data["values"][0]
        await self.map_to(interaction, account)

    async def _on_custom_mapping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CustomMappingModal(self))

    async def _on_nearby(self, interaction: discord.Interaction) -> None:
        lat = float(self.location["lat"])
        lon = float(self.location["lon"])
        proc = await asyncio.create_subprocess_exec(
            NEARBY_BIN,
            str(lat),
            str(lon),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = stderr.decode().strip() or "Nearby lookup failed"
            await interaction.response.send_message(message, ephemeral=True)
            return
        lines = [line for line in stdout.decode().splitlines() if line.strip()]
        if not lines:
            await interaction.response.send_message(
                "No nearby places found.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="Nearby merchants",
            description="\n".join(f"• {line}" for line in lines[:10]),
            color=discord.Color.blue(),
        )
        view = NearbySuggestionsView(self, lines)
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    async def map_to(
        self,
        interaction: discord.Interaction,
        account: str,
        *,
        table: str = "payees",
        key: str | None = None,
    ) -> None:
        mapping_key = key or self._pending_key or self.payee
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            async with _mapping_lock:
                await asyncio.to_thread(
                    upsert_mapping,
                    self.config.mappings_path,
                    table,
                    mapping_key,
                    account,
                )
                mappings = await asyncio.to_thread(
                    Mappings.load, self.config.mappings_path
                )
                await asyncio.to_thread(
                    rebuild_journal,
                    self.config.ingest_log_path,
                    self.config.journal_dir,
                    mappings,
                )
        except (ValueError, OSError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        embed = discord.Embed(
            title=f"Mapped {mapping_key}",
            description=f"Account: `{account}`",
            color=discord.Color.green(),
        )
        if interaction.message is not None:
            await interaction.message.edit(embed=embed, view=None)
        else:
            await interaction.followup.send(embed=embed)


def next_weekly_digest(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    days_ahead = (0 - now.weekday()) % 7
    target = (now + timedelta(days=days_ahead)).replace(
        hour=8, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += timedelta(days=7)
    return target


def next_month_end_digest(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    else:
        now = now.astimezone(IST)
    target = now.replace(day=1, hour=8, minute=0, second=0, microsecond=0)
    if target <= now:
        if now.month == 12:
            target = target.replace(year=now.year + 1, month=1)
        else:
            target = target.replace(month=now.month + 1)
    return target


def build_digest_sections(
    config: BotConfig, period: str
) -> tuple[str, str, int]:
    categories = spending_report(period, None, ledger_file=config.ledger_file)
    top_payees = run_hledger(
        ["bal", "expenses", "-p", period, "--pivot", "payee", "-S", "-N"],
        ledger_file=config.ledger_file,
    )
    unknown_count = len(collect_unknown_payees(config))
    return categories, top_payees, unknown_count


class ReviewUnmappedView(discord.ui.View):
    def __init__(self, config: BotConfig) -> None:
        super().__init__(timeout=None)
        self.config = config

    @discord.ui.button(
        label="Review Unmapped",
        style=discord.ButtonStyle.primary,
        custom_id="digest:review",
    )
    async def review_unmapped(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await review_command(interaction, self.config)


async def send_scheduled_digest(
    bot: commands.Bot,
    config: BotConfig,
    *,
    title: str,
    period: str,
) -> None:
    channel = bot.get_channel(config.home_channel)
    if channel is None:
        return
    try:
        categories, top_payees, unknown_count = await asyncio.to_thread(
            build_digest_sections, config, period
        )
    except HledgerError as exc:
        await channel.send(f"Digest failed: {exc}")
        return

    embed = discord.Embed(title=title, color=discord.Color.blue())
    embed.add_field(name="Unknown payees", value=str(unknown_count), inline=True)
    view = ReviewUnmappedView(config) if unknown_count > 0 else None
    await channel.send(embed=embed, view=view)
    for label, text in (("By category", categories), ("Top payees", top_payees)):
        for chunk in chunk_text(text) or ["(no data)"]:
            await channel.send(f"**{label}**\n```\n{chunk}\n```")


async def post_weekly_digest(bot: commands.Bot, config: BotConfig) -> None:
    await send_scheduled_digest(
        bot, config, title="Weekly spending digest", period="lastweek"
    )


async def post_month_end_digest(bot: commands.Bot, config: BotConfig) -> None:
    await send_scheduled_digest(
        bot, config, title="Month-end spending digest", period="lastmonth"
    )


def _start_digest_tasks(bot: commands.Bot, config: BotConfig) -> None:
    @tasks.loop(time=time(8, 0, tzinfo=IST))
    async def weekly_digest_task() -> None:
        if datetime.now(IST).weekday() != 0:
            return
        await post_weekly_digest(bot, config)

    @tasks.loop(time=time(8, 0, tzinfo=IST))
    async def month_end_digest_task() -> None:
        if datetime.now(IST).day != 1:
            return
        await post_month_end_digest(bot, config)

    @weekly_digest_task.before_loop
    async def before_weekly() -> None:
        await bot.wait_until_ready()

    @month_end_digest_task.before_loop
    async def before_month_end() -> None:
        await bot.wait_until_ready()

    weekly_digest_task.start()
    month_end_digest_task.start()


async def review_command(
    interaction: discord.Interaction,
    config: BotConfig,
) -> None:
    await interaction.response.defer()
    candidates = await asyncio.to_thread(collect_unknown_payees, config)
    if not candidates:
        await interaction.followup.send(
            "No unknown payees to review.", ephemeral=True
        )
        return
    for candidate in candidates:
        embed = build_review_embed(candidate)
        view = PayeeReviewView(
            candidate.payee,
            config,
            location=candidate.location,
        )
        await interaction.followup.send(embed=embed, view=view)


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

    @bot.tree.command(
        name="review",
        description="Review unmapped payees and assign categories",
    )
    async def review(interaction: discord.Interaction) -> None:
        await review_command(interaction, config)


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
    async def on_ready() -> None:
        bot.add_view(ReviewUnmappedView(config))
        _start_digest_tasks(bot, config)

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
