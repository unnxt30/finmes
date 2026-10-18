"""Discord confirmation cards and a conversational bridge to Hermes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import discord
from discord import app_commands

from finmes.enrichment import CATEGORIES
from finmes.journal import Mappings
from finmes.reviews import ReviewEngine

log = logging.getLogger(__name__)


def review_embed(row: dict) -> discord.Embed:
    candidate = row["candidate"]
    examples = "\n".join(
        f"{e['date']} · {discord.utils.escape_markdown(e['amount'])} · {e['direction']}"
        for e in candidate["examples"])
    embed = discord.Embed(title=f"Review: {candidate['payee']}"[:256],
                          description=f"{candidate['count']} uncategorized transaction(s)\n{examples}",
                          color=discord.Color.orange())
    embed.add_field(name="Category to confirm", value=row["category"] or "Choose a category below.", inline=False)
    if row["confidence"] is not None:
        embed.add_field(name="AI confidence", value=f"{row['confidence']:.0%}")
    if row["places"]:
        lines = []
        for p in row["places"][:3]:
            distance = p.get("distance_m")
            suffix = f" ({distance} m)" if isinstance(distance, (int, float)) else ""
            lines.append(discord.utils.escape_markdown(p["name"]) + suffix)
        embed.add_field(name="Nearby possibilities — unverified", value="\n".join(lines)[:1024], inline=False)
    embed.add_field(name="Suggestion context", value=row["note"][:1024] or "Awaiting suggestion.", inline=False)
    embed.set_footer(text="Confirm saves this payee mapping for future transactions and updates matching unknown history.")
    return embed


async def interaction_error(interaction: discord.Interaction, exc: Exception):
    message = str(exc) if isinstance(exc, (ValueError, PermissionError)) else "Review failed; please retry /review."
    if not interaction.response.is_done():
        await interaction.response.send_message(message, ephemeral=True)
    else:
        await interaction.followup.send(message, ephemeral=True)


class CategoryModal(discord.ui.Modal, title="Choose a category"):
    account = discord.ui.TextInput(label="Category account", placeholder="expenses:groceries", max_length=100)

    def __init__(self, view: ReviewView):
        super().__init__(custom_id=f"review:modal:{view.review_id}:{view.version}"
                         + (f":{view.batch_id}" if view.batch_id else ""))
        self.review = view

    async def on_submit(self, interaction: discord.Interaction):
        await self.review.choose(interaction, self.account.value.strip())

    async def on_error(self, interaction, error):
        log.exception("Category form failed", exc_info=error)
        await interaction_error(interaction, error)


def category_choices(engine: ReviewEngine) -> list[tuple[str, str]]:
    """Confirmed mapping accounts become presets; unsaved selections do not."""
    choices = list(CATEGORIES)
    known = {category for category, _ in choices}
    custom = set(Mappings.load(engine.mappings_path).payees.values()) - known
    choices.extend((category, category) for category in sorted(custom))
    return choices


class ReviewView(discord.ui.View):
    def __init__(self, engine: ReviewEngine, row: dict, batch_id: str | None = None,
                 category_page: int | None = None):
        super().__init__(timeout=None)
        self.engine, self.review_id, self.version = engine, row["id"], row["version"]
        self.batch_id = batch_id
        prefix = f"{self.review_id}:{self.version}" + (f":{batch_id}" if batch_id else "")
        choices = category_choices(engine)
        if row["category"] and row["category"] not in {c for c, _ in choices}:
            choices.append((row["category"], row["category"]))
        selected_index = next((i for i, (c, _) in enumerate(choices) if c == row["category"]), 0)
        self.category_page = selected_index // 25 if category_page is None else category_page
        self.category_page = min(self.category_page, (len(choices) - 1) // 25)
        self.category_pages = (len(choices) + 24) // 25
        if self.category_page:
            prefix += f":p{self.category_page}"
        select = discord.ui.Select(placeholder=f"Choose category ({self.category_page + 1}/{self.category_pages})",
            custom_id=f"review:select:{prefix}", row=0,
            options=[discord.SelectOption(label=description[:100], value=category,
                      default=category == row["category"])
                     for category, description in choices[self.category_page * 25:(self.category_page + 1) * 25]])
        select.callback = self.on_select
        self.add_item(select)
        if self.category_pages > 1:
            for name, label, offset in (("previous", "Previous categories", -1), ("next", "More categories", 1)):
                button = discord.ui.Button(label=label, custom_id=f"review:{name}:{prefix}", row=1,
                    disabled=not 0 <= self.category_page + offset < self.category_pages)
                async def page_callback(interaction, offset=offset):
                    try:
                        self.engine.authorize(interaction.user.id)
                        await interaction.response.defer()
                        current = self.engine.get(self.review_id)
                        self.engine._check(current, self.version)
                        view = ReviewView(self.engine, current, self.batch_id, self.category_page + offset)
                        await interaction.edit_original_response(embed=view.card_embed(current), view=view)
                    except (ValueError, PermissionError) as exc:
                        await interaction_error(interaction, exc)
                button.callback = page_callback
                self.add_item(button)
        for name, label, callback, style in (
            ("confirm", "Confirm category", self.on_confirm, discord.ButtonStyle.success),
            ("custom", "Other category", self.on_custom, discord.ButtonStyle.secondary),
            ("refresh", "Refresh suggestion", self.on_refresh, discord.ButtonStyle.secondary),
            ("skip", "Dismiss", self.on_skip, discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=label, custom_id=f"review:{name}:{prefix}", style=style,
                                       row=2 if self.category_pages > 1 else 1, disabled=name == "confirm" and not row["category"])
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            self.engine.authorize(interaction.user.id)
            return True
        except PermissionError as exc:
            await interaction_error(interaction, exc)
            return False

    async def on_error(self, interaction, error, item):
        log.exception("Review interaction failed", exc_info=error)
        await interaction_error(interaction, error)

    async def choose(self, interaction: discord.Interaction, account: str):
        try:
            self.engine.authorize(interaction.user.id)
            await interaction.response.defer()
            row = await asyncio.to_thread(self.engine.choose, self.review_id, self.version, interaction.user.id, account)
            if interaction.message is not None:
                await interaction.edit_original_response(embed=self.card_embed(row), view=ReviewView(self.engine, row, self.batch_id))
            else:
                await interaction.followup.send(embed=self.card_embed(row), view=ReviewView(self.engine, row, self.batch_id), ephemeral=True)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def on_select(self, interaction: discord.Interaction):
        await self.choose(interaction, interaction.data["values"][0])

    async def on_custom(self, interaction: discord.Interaction):
        self.engine.authorize(interaction.user.id)
        await interaction.response.send_modal(CategoryModal(self))

    async def on_refresh(self, interaction: discord.Interaction):
        try:
            self.engine.authorize(interaction.user.id)
            await interaction.response.defer()
            row = await asyncio.to_thread(self.engine.refresh, self.review_id, self.version, interaction.user.id)
            await interaction.edit_original_response(embed=self.card_embed(row), view=ReviewView(self.engine, row, self.batch_id))
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    def card_embed(self, row: dict) -> discord.Embed:
        embed = review_embed(row)
        if self.batch_id:
            batch = self.engine.batch(self.batch_id)
            embed.add_field(name="Review progress",
                            value=f"{batch['position'] + 1} of {len(batch['queue'])} payees", inline=False)
        return embed

    async def finish_card(self, interaction: discord.Interaction, message: str):
        # Acknowledge the successful write before making another provider request.
        await interaction.edit_original_response(content=message, embed=None, view=None,
                                                  allowed_mentions=discord.AllowedMentions.none())
        if not self.batch_id:
            return
        try:
            row = await asyncio.to_thread(self.engine.batch_next, self.batch_id)
            if row is None:
                await interaction.edit_original_response(content=message + "\nReview session complete.")
                return
            row = await asyncio.to_thread(self.engine.prepare, row["id"])
            view = ReviewView(self.engine, row, self.batch_id)
            await interaction.edit_original_response(content=message, embed=view.card_embed(row), view=view)
        except Exception:
            log.exception("Could not load next review after successful action")
            await interaction.followup.send(
                "Your previous action succeeded. Could not load the next payee; run /review to continue.",
                ephemeral=True)

    async def on_confirm(self, interaction: discord.Interaction):
        try:
            self.engine.authorize(interaction.user.id)
            await interaction.response.defer()
            changed = await asyncio.to_thread(self.engine.confirm, self.review_id, self.version, interaction.user.id)
            row = self.engine.get(self.review_id)
            await self.finish_card(interaction,
                f"Saved **{discord.utils.escape_markdown(row['payee'])} → {row['category']}**. Updated {changed} unknown transaction(s).")
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def on_skip(self, interaction: discord.Interaction):
        try:
            self.engine.authorize(interaction.user.id)
            await interaction.response.defer()
            await asyncio.to_thread(self.engine.skip, self.review_id, self.version, interaction.user.id)
            await self.finish_card(interaction, "Dismissed. No category mapping was saved. Use /review with this payee to reopen it.")
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)


class HermesClient:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    def answer(self, question: str, session_key: str) -> str:
        req = Request(self.url + "/v1/chat/completions",
            data=json.dumps({"model": "hermes-agent", "messages": [{"role": "user", "content": question}]}).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                     "X-Hermes-Session-Key": session_key})
        try:
            with urlopen(req, timeout=180) as response:
                body = json.load(response)
            return body["choices"][0]["message"]["content"] or "Hermes returned an empty response."
        except HTTPError as exc:
            return f"Hermes request failed (HTTP {exc.code}); please retry."
        except (URLError, OSError, ValueError, KeyError, IndexError, TypeError):
            return "Hermes could not complete that request; please retry."


class FinanceBot(discord.Client):
    def __init__(self, engine: ReviewEngine, hermes: HermesClient, home_channel: int | None,
                 *, auto_reviews: bool = False):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.engine, self.hermes, self.home_channel = engine, hermes, home_channel
        self.tree = app_commands.CommandTree(self)
        self.chat_locks = {}
        self.watch_task = None
        self.auto_reviews = auto_reviews

        @self.tree.command(name="review", description="Review a suggested category; confirmation is required to save")
        @app_commands.describe(payee="Optional exact payee name, including a previously dismissed payee",
                               count="Review up to this many payees one by one (1–50); omit for all pending")
        async def review(interaction: discord.Interaction, payee: str | None = None,
                         count: app_commands.Range[int, 1, 50] | None = None):
            try:
                engine.authorize(interaction.user.id)
                await interaction.response.defer(ephemeral=True)
                await asyncio.to_thread(engine.scan)
                row = await asyncio.to_thread(engine.next, payee=payee)
                if row is None and payee:
                    row = await asyncio.to_thread(engine.reopen, payee, interaction.user.id)
                if row is None:
                    await interaction.followup.send("No matching unknown payees to review.", ephemeral=True)
                    return
                row = await asyncio.to_thread(engine.prepare, row["id"])
                batch_id = await asyncio.to_thread(engine.start_batch, interaction.user.id, row, count, single_payee=bool(payee))
                view = ReviewView(engine, row, batch_id)
                await interaction.followup.send(embed=view.card_embed(row), view=view, ephemeral=True)
            except (ValueError, PermissionError) as exc:
                await interaction_error(interaction, exc)

        @self.tree.error
        async def command_error(interaction, error):
            log.exception("Review command failed", exc_info=error)
            await interaction_error(interaction, getattr(error, "original", error))

    def restore_review_views(self, row: dict, batch_id: str | None = None):
        first = ReviewView(self.engine, row, batch_id, 0)
        self.add_view(first)
        for page in range(1, first.category_pages):
            self.add_view(ReviewView(self.engine, row, batch_id, page))

    async def setup_hook(self):
        await asyncio.to_thread(self.engine.scan)
        for row in await asyncio.to_thread(self.engine.pending):
            self.restore_review_views(row)
        for batch_id in await asyncio.to_thread(self.engine.active_batches):
            row = await asyncio.to_thread(self.engine.batch_next, batch_id)
            if row and row["state"] == "pending":
                self.restore_review_views(row, batch_id)
        await self.tree.sync()
        if self.auto_reviews:
            self.watch_task = asyncio.create_task(self.watch_reviews())

    async def on_ready(self):
        log.info("Discord connected as %s; /review registered", self.user)

    async def watch_reviews(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await asyncio.to_thread(self.engine.scan)
                row = await asyncio.to_thread(self.engine.next, automatic=True)
                if row:
                    row = await asyncio.to_thread(self.engine.prepare, row["id"])
                    if row["state"] == "pending":
                        if self.home_channel:
                            target = self.get_channel(self.home_channel) or await self.fetch_channel(self.home_channel)
                        else:
                            target = await self.fetch_user(min(self.engine.allowed_users))
                        await target.send(embed=review_embed(row), view=ReviewView(self.engine, row))
                        await asyncio.to_thread(self.engine.mark_notified, row["id"])
            except Exception:
                log.exception("Background category review failed; will retry")
            await asyncio.sleep(30)

    async def on_message(self, message: discord.Message):
        if message.author.bot or message.author.id not in self.engine.allowed_users:
            return
        question = message.content.strip()
        if message.guild is not None:
            if self.user not in message.mentions:
                return
            question = question.replace(f"<@{self.user.id}>", "").replace(f"<@!{self.user.id}>", "").strip()
        if not question:
            return
        session_key = f"finmes:discord:{message.channel.id}:{message.author.id}"
        lock = self.chat_locks.setdefault(session_key, asyncio.Lock())
        async with lock, message.channel.typing():
            answer = await asyncio.to_thread(self.hermes.answer, question, session_key)
        for start in range(0, len(answer), 1900):
            await message.reply(answer[start:start + 1900], mention_author=False)

    async def close(self):
        if self.watch_task:
            self.watch_task.cancel()
        await super().close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    allowed = {int(x.strip()) for x in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",") if x.strip()}
    key = os.environ.get("API_SERVER_KEY", "")
    if not token or not allowed or not key:
        raise SystemExit("DISCORD_BOT_TOKEN, DISCORD_ALLOWED_USERS, and API_SERVER_KEY are required")
    engine = ReviewEngine(Path(os.environ.get("FINMES_DATA_DIR", "/app/data")), allowed,
                          openai_key=os.environ.get("OPENAI_API_KEY", ""), mappls_key=os.environ.get("MAPPLS_API_KEY", ""))
    bot = FinanceBot(engine, HermesClient(os.environ.get("HERMES_URL", "http://hermes:8642"), key),
                     int(os.environ["DISCORD_HOME_CHANNEL"]) if os.environ.get("DISCORD_HOME_CHANNEL") else None,
                     auto_reviews=os.environ.get("DISCORD_REVIEW_NOTIFICATIONS", "false").lower() == "true")
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
