"""Private Discord controls for payment confirmations and historical matches."""

import asyncio
import logging

import discord

from finmes.discord_bot import interaction_error, send_private_review
from finmes.enrichment import EnrichmentError

log = logging.getLogger(__name__)


def payment_label(payment):
    return f"{payment['date']} · {payment['amount']} · {payment['direction']}"


class PrivatePaymentView(discord.ui.View):
    def __init__(self, actor, *, timeout):
        super().__init__(timeout=timeout)
        self.actor = actor

    async def interaction_check(self, interaction):
        if interaction.user.id == self.actor:
            return True
        await interaction_error(interaction, PermissionError("This payment review belongs to another user."))
        return False

    async def on_error(self, interaction, error, item):
        log.exception("Payment review failed", exc_info=error)
        await interaction_error(interaction, error)

    def add_button(self, label, callback, *, custom_id=None, style=discord.ButtonStyle.secondary, disabled=False):
        button = discord.ui.Button(label=label, custom_id=custom_id, style=style, disabled=disabled, row=1)
        button.callback = callback
        self.add_item(button)


class PaymentPicker(PrivatePaymentView):
    PAGE_SIZE = 25

    def __init__(self, store, review_id, version, actor, payee, category, payments, page=0, selected=None):
        super().__init__(actor, timeout=300)
        self.store, self.review_id, self.version = store, review_id, version
        self.payee, self.category, self.payments = payee, category, payments
        self.page, self.selected = page, selected
        start = page * self.PAGE_SIZE
        select = discord.ui.Select(placeholder="Choose a payment", row=0, options=[
            discord.SelectOption(label=payment_label(payment)[:100], value=str(index),
                                 default=index == selected)
            for index, payment in enumerate(payments[start:start + self.PAGE_SIZE], start)])
        select.callback = self.select_payment
        self.add_item(select)
        if len(payments) > self.PAGE_SIZE:
            self.add_button("Previous", self.previous_page, disabled=page == 0)
            self.add_button("Next", self.next_page, disabled=start + self.PAGE_SIZE >= len(payments))
        self.add_button("Save this payment", self.confirm, style=discord.ButtonStyle.success, disabled=selected is None)

    def embed(self):
        description = f"Category **{discord.utils.escape_markdown(self.category)}**"
        if self.selected is not None:
            description += "\n" + discord.utils.escape_markdown(payment_label(self.payments[self.selected]))
        return discord.Embed(title=f"Confirm one payment to {self.payee}"[:256], description=description)

    async def show(self, interaction, page, selected=None):
        view = PaymentPicker(self.store, self.review_id, self.version, self.actor, self.payee,
                             self.category, self.payments, page, selected)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def select_payment(self, interaction):
        await self.show(interaction, self.page, int(interaction.data["values"][0]))

    async def previous_page(self, interaction):
        await self.show(interaction, self.page - 1)

    async def next_page(self, interaction):
        await self.show(interaction, self.page + 1)

    async def confirm(self, interaction):
        if self.selected is None:
            return
        await interaction.response.defer()
        try:
            payment, category = await asyncio.to_thread(
                self.store.confirm, self.review_id, self.version, interaction.user.id,
                self.payments[self.selected]["key"])
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)
            return

        saved = (f"Saved **{discord.utils.escape_markdown(payment_label(payment))}** as "
                 f"**{discord.utils.escape_markdown(category)}**. Run /review for an updated payee list.")
        await interaction.edit_original_response(content=saved + "\nChecking older payments for matches…",
                                                 embed=None, view=None)
        try:
            batch, checked = await asyncio.to_thread(self.store.suggest, interaction.user.id, payment, category)
        except EnrichmentError as exc:
            await interaction.edit_original_response(content=saved + f"\nDecisions could not check older payments ({exc}).")
            return
        except Exception:
            log.exception("Finding historical payment matches failed after confirmation")
            await interaction.edit_original_response(content=saved + "\nCould not check older payments.")
            return
        if batch is None:
            await interaction.edit_original_response(content=saved + f"\nChecked {checked} uncategorized payments and found no high-confidence matches.")
            return
        await interaction.edit_original_response(content=saved)
        await send_private_review(interaction, embed=matches_embed(batch), view=PaymentMatches(self.store, batch))


def matches_embed(batch):
    description = f"{batch['checked']} uncategorized payments checked"
    if batch["category"]:
        description = f"**{discord.utils.escape_markdown(batch['category'])}** · {description}"
    embed = discord.Embed(title="Suggested historical matches",
                          description=description)
    for match in batch["matches"]:
        status = "Included" if match["included"] else "Excluded"
        details = f"{payment_label(match)} · AI {match['probability']:.0%} · {status}"
        if not batch["category"]:
            details += f"\n{discord.utils.escape_markdown(match['category'])}"
        embed.add_field(name=discord.utils.escape_markdown(match["payee"])[:256],
                        value=details[:1024], inline=False)
    return embed


class PaymentMatches(PrivatePaymentView):
    def __init__(self, store, batch):
        super().__init__(batch["actor"], timeout=None)
        self.store, self.batch_id, self.version = store, batch["id"], batch["version"]
        prefix = f"payment-match:{self.batch_id}:{self.version}"
        select = discord.ui.Select(placeholder="Choose which matches to include", custom_id=f"{prefix}:select",
                                   min_values=0, max_values=len(batch["matches"]), row=0, options=[
            discord.SelectOption(label=f"{match['payee']} · {match['amount']}"[:100],
                                 description=f"{match['date']} · {match.get('category', batch['category'])}"[:100],
                                 value=match["key"], default=match["included"])
            for match in batch["matches"]])
        select.callback = self.select_matches
        self.add_item(select)
        count = sum(match["included"] for match in batch["matches"])
        self.add_button(f"Approve {count} payments", self.approve, custom_id=f"{prefix}:approve",
                        style=discord.ButtonStyle.success, disabled=count == 0)
        self.add_button("Dismiss matches", self.dismiss, custom_id=f"{prefix}:dismiss")

    async def select_matches(self, interaction):
        await interaction.response.defer()
        try:
            batch = await asyncio.to_thread(self.store.select, self.batch_id, interaction.user.id, self.version,
                                            set(interaction.data["values"]))
            await interaction.edit_original_response(embed=matches_embed(batch), view=PaymentMatches(self.store, batch))
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def approve(self, interaction):
        await self.finish(interaction, approve=True)

    async def dismiss(self, interaction):
        await self.finish(interaction, approve=False)

    async def finish(self, interaction, *, approve):
        await interaction.response.defer()
        try:
            changed, skipped = await asyncio.to_thread(self.store.finish, self.batch_id, interaction.user.id,
                                                       self.version, approve=approve)
            message = f"Categorized {changed} historical payments." if approve else "Dismissed the proposed matches."
            if approve and skipped:
                message += f" {skipped} selected payments had already changed."
            await interaction.edit_original_response(content=message, embed=None, view=None)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)
