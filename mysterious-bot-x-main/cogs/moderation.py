"""
cogs/moderation.py — ModerationCog

Registers the /mod command group, context menus (punish / history),
and the on_raw_reaction_add listener used by legacy case panels.

Guild context is set before every event handler so that bot.data_manager
reads from the correct guild's store.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from modules import mbx_moderation
from storage import set_guild_store, reset_guild_store

logger = logging.getLogger("MGXBot.cogs.moderation")


class ModerationCog(commands.Cog, name="Moderation"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        token = None
        if payload.guild_id and self.bot.guild_manager:
            store = self.bot.guild_manager.get_store(payload.guild_id)
            token = set_guild_store(store)
        try:
            await mbx_moderation.on_raw_reaction_add(payload)
        finally:
            if token is not None:
                reset_guild_store(token)


async def setup(bot: commands.Bot) -> None:
    cog = ModerationCog(bot)
    await bot.add_cog(cog)

    bot.tree.add_command(mbx_moderation.ModGroup())
    bot.tree.add_command(mbx_moderation.punish_context)
    bot.tree.add_command(mbx_moderation.history_context)
