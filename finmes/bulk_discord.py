"""Private batch summaries and edit controls for explicit category approval."""
import asyncio
import logging
from decimal import Decimal

import discord

from finmes.discord_bot import ReviewView, interaction_error, review_embed, send_private_review


log = logging.getLogger(__name__)


def summary_embed(batch):
    embed = discord.Embed(title='Review category suggestions', color=discord.Color.orange(),
        description='Check the suggestions, edit exceptions, then approve the included mappings.\n'
                    'Confidence is a model estimate, not proof. No categories save until approval.')
    for index, item in enumerate(batch['items']):
        candidate = item['candidate']
        totals = ', '.join(f'{currency} {Decimal(value):,.2f}'
                           for currency, value in sorted(candidate.get('totals', {}).items())) or 'Total unavailable'
        status = 'Included' if item['included'] else 'Needs input / excluded'
        confidence = f" · AI {item['confidence']:.0%}" if item['confidence'] is not None else ''
        category = discord.utils.escape_markdown(item['category'] or 'Unknown')
        embed.add_field(name=f"{index + 1}. {candidate['payee']}"[:120],
                        value=f"{candidate['count']} transactions · {totals}\n{category}\n{status}{confidence}"[:400],
                        inline=False)
    embed.set_footer(text='Unknown or excluded payees stay pending. Close saves nothing.')
    return embed


async def acknowledge(interaction):
    if not interaction.response.is_done():
        await interaction.response.defer()


class BulkSummary(discord.ui.View):
    def __init__(self, store, batch):
        super().__init__(timeout=None)
        self.store, self.batch_id, self.version = store, batch['id'], batch['version']
        prefix = f"{self.batch_id}:{self.version}"
        select = discord.ui.Select(placeholder='Edit or exclude a payee', custom_id=f'bulk:edit:{prefix}',
            options=[discord.SelectOption(label=f"{i + 1}. {item['candidate']['payee']}"[:100], value=str(i))
                     for i, item in enumerate(batch['items'])], row=0)
        select.callback = self.edit
        self.add_item(select)
        included = sum(bool(i['included'] and i['category']) for i in batch['items'])
        for name, label, callback in [('approve', f'Approve {included} payee mappings', self.approve),
                                      ('close', 'Close without saving', self.close_batch)]:
            button = discord.ui.Button(label=label, custom_id=f'bulk:{name}:{prefix}', row=1,
                style=discord.ButtonStyle.success if name == 'approve' else discord.ButtonStyle.secondary,
                disabled=name == 'approve' and included == 0)
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction):
        try:
            await acknowledge(interaction)
            await asyncio.to_thread(self.store.get, self.batch_id, interaction.user.id, self.version)
            return True
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)
            return False

    async def on_error(self, interaction, error, item):
        log.exception("Bulk review interaction failed", exc_info=error)
        await interaction_error(interaction, error)

    async def edit(self, interaction):
        try:
            await acknowledge(interaction)
            batch = await asyncio.to_thread(self.store.get, self.batch_id, interaction.user.id, self.version)
            if batch['state'] != 'open':
                raise ValueError('This batch is closed; run /review again.')
            index = int(interaction.data['values'][0])
            item = batch['items'][index]
            row = self.store.engine.get(item['id'])
            self.store.engine._check(row, item['version'])
            view = BulkItem(self.store, batch, index, row)
            await interaction.edit_original_response(embed=view.card_embed(row), view=view, content=None)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def approve(self, interaction):
        try:
            await acknowledge(interaction)
            result = await asyncio.to_thread(self.store.approve, self.batch_id, interaction.user.id, self.version)
            message = (f"Saved {len(result['saved'])} payee mappings; updated {result['changed']} unknown transactions. "
                       f"{result['pending']} payees from this batch remain pending.")
            if result['failed']:
                message += ('\nSome saves did not finish; run /review to retry pending payees. '
                            'A mapping may already have saved before a historical update failed.')
            await interaction.edit_original_response(content=message, embed=None, view=None)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def close_batch(self, interaction):
        try:
            await acknowledge(interaction)
            self.store.close(self.batch_id, interaction.user.id, self.version)
            await interaction.edit_original_response(content='Batch closed. No category mappings saved; payees remain pending.',
                                                     embed=None, view=None)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)


class BulkItem(ReviewView):
    def __init__(self, store, batch, index, row, category_page=None):
        self.store, self.bulk_id, self.index, self.owner = store, batch['id'], index, batch['actor']
        super().__init__(store.engine, row, batch['id'], category_page)
        for item in self.children:
            item.custom_id = item.custom_id.replace('review:', 'bulkitem:', 1)
            if ':confirm:' in item.custom_id:
                item.label = 'Include and return'
            elif ':skip:' in item.custom_id:
                item.label = 'Exclude and return'
        back = discord.ui.Button(label='Back to summary', custom_id=f'bulkitem:back:{self.bulk_id}:{self.review_id}',
                                 row=3, style=discord.ButtonStyle.secondary)
        back.callback = self.back
        self.add_item(back)
        # Replace category-page callbacks, which would otherwise return a single-review view.
        for item in self.children:
            if ':previous:' in item.custom_id or ':next:' in item.custom_id:
                offset = -1 if ':previous:' in item.custom_id else 1
                async def page(interaction, offset=offset):
                    self.store.get(self.bulk_id, interaction.user.id)
                    await interaction.response.defer()
                    row = self.engine.get(self.review_id)
                    self.engine._check(row, self.version)
                    batch = self.store.get(self.bulk_id, interaction.user.id)
                    view = BulkItem(self.store, batch, self.index, row, self.category_page + offset)
                    await interaction.edit_original_response(embed=view.card_embed(row), view=view)
                item.callback = page

    async def interaction_check(self, interaction):
        try:
            batch = self.store.get(self.bulk_id, interaction.user.id)
            if batch['state'] != 'open':
                raise ValueError('This batch is closed; run /review again.')
            return await super().interaction_check(interaction)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)
            return False

    def card_embed(self, row):
        embed = review_embed(row)
        embed.set_footer(text='Edits only update this proposal. Return to the summary and approve to save mappings.')
        return embed

    async def show_summary(self, interaction, batch):
        await interaction.edit_original_response(embed=summary_embed(batch), view=BulkSummary(self.store, batch), content=None)

    async def choose(self, interaction, account):
        try:
            self.store.get(self.bulk_id, interaction.user.id)
            await interaction.response.defer()
            row = await asyncio.to_thread(self.engine.choose, self.review_id, self.version, interaction.user.id, account)
            batch = self.store.update(self.bulk_id, interaction.user.id, self.index, row, manual=True)
            view = BulkItem(self.store, batch, self.index, row)
            if interaction.message is not None:
                await interaction.edit_original_response(embed=view.card_embed(row), view=view)
            else:
                await send_private_review(interaction, embed=view.card_embed(row), view=view)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def on_refresh(self, interaction):
        try:
            self.store.get(self.bulk_id, interaction.user.id)
            await interaction.response.defer()
            row = await asyncio.to_thread(self.engine.refresh, self.review_id, self.version, interaction.user.id)
            batch = self.store.update(self.bulk_id, interaction.user.id, self.index, row)
            view = BulkItem(self.store, batch, self.index, row)
            await interaction.edit_original_response(embed=view.card_embed(row), view=view)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def on_confirm(self, interaction):
        await self.set_included(interaction, True)

    async def on_skip(self, interaction):
        await self.set_included(interaction, False)

    async def set_included(self, interaction, included):
        try:
            self.store.get(self.bulk_id, interaction.user.id)
            await interaction.response.defer()
            row = self.engine.get(self.review_id)
            self.engine._check(row, self.version)
            batch = self.store.update(self.bulk_id, interaction.user.id, self.index, row, include=included)
            await self.show_summary(interaction, batch)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)

    async def back(self, interaction):
        try:
            batch = self.store.get(self.bulk_id, interaction.user.id)
            await interaction.response.defer()
            await self.show_summary(interaction, batch)
        except (ValueError, PermissionError) as exc:
            await interaction_error(interaction, exc)
