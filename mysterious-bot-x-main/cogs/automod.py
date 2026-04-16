"""
cogs/automod.py — AutoModCog

Listens to on_automod_action and on_socket_raw_receive for the native
Discord AutoMod bridge.  Guild context is set before each dispatch so
that all helper functions get the right guild's config.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from modules import mbx_automod
from storage import set_guild_store, reset_guild_store

logger = logging.getLogger("MGXBot.cogs.automod")


class AutoModCog(commands.Cog, name="AutoMod"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction) -> None:
        token = None
        if execution.guild_id and self.bot.guild_manager:
            store = self.bot.guild_manager.get_store(execution.guild_id)
            token = set_guild_store(store)
        try:
            await mbx_automod.on_automod_action(execution)
        finally:
            if token is not None:
                reset_guild_store(token)

    @commands.Cog.listener()
    async def on_socket_raw_receive(self, msg) -> None:
        # Guild context is resolved inside the handler based on the payload
        await mbx_automod.on_socket_raw_receive(msg)


async def setup(bot: commands.Bot) -> None:
    cog = AutoModCog(bot)
    await bot.add_cog(cog)

    bot.tree.add_command(mbx_automod.automod_cmd)
