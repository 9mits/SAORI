"""
cogs/system.py — SystemCog

Handles: on_ready, on_message, on_member_update, on_guild_role_update,
         app_command_error, and all /setup /config /stats … slash commands.

Guild context is set at the top of every event handler so that all legacy
helper functions that read bot.data_manager.config get the right guild's data.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from modules import mbx_system
from storage import set_guild_store, reset_guild_store

logger = logging.getLogger("MGXBot.cogs.system")


class SystemCog(commands.Cog, name="System"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── ready ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Load all guild stores, then run the legacy on_ready hook."""
        if self.bot.guild_manager:
            await self.bot.guild_manager.load_all_guilds()
        await mbx_system.on_ready()

    # ── guild join / leave ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self.bot.guild_manager:
            await self.bot.guild_manager.on_guild_join(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if self.bot.guild_manager:
            self.bot.guild_manager.on_guild_remove(guild)

    # ── message ────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        token = None
        if message.guild and self.bot.guild_manager:
            store = self.bot.guild_manager.get_store(message.guild.id)
            token = set_guild_store(store)
        elif isinstance(message.channel, discord.DMChannel) and self.bot.guild_manager:
            # DM: look up the guild via the modmail routing table
            guild_id = self.bot.guild_manager.get_dm_guild_id(str(message.author.id))
            if guild_id:
                store = self.bot.guild_manager.get_store(guild_id)
                token = set_guild_store(store)
            # If no routing found yet, legacy code will fall back gracefully
        try:
            await mbx_system.on_message(message)
        finally:
            if token is not None:
                reset_guild_store(token)

    # ── member / role updates ─────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        token = None
        if self.bot.guild_manager:
            store = self.bot.guild_manager.get_store(after.guild.id)
            token = set_guild_store(store)
        try:
            await mbx_system.on_member_update(before, after)
        finally:
            if token is not None:
                reset_guild_store(token)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        token = None
        if self.bot.guild_manager:
            store = self.bot.guild_manager.get_store(after.guild.id)
            token = set_guild_store(store)
        try:
            await mbx_system.on_guild_role_update(before, after)
        finally:
            if token is not None:
                reset_guild_store(token)

    # ── app command error ─────────────────────────────────────────────────

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await mbx_system.on_app_command_error(interaction, error)


async def setup(bot: commands.Bot) -> None:
    cog = SystemCog(bot)
    await bot.add_cog(cog)

    # Register slash commands
    bot.tree.add_command(mbx_system.list_commands)
    bot.tree.add_command(mbx_system.stats)
    bot.tree.add_command(mbx_system.directory)
    bot.tree.add_command(mbx_system.setup)
    bot.tree.add_command(mbx_system.config_cmd)
    bot.tree.add_command(mbx_system.publicexecution)
    bot.tree.add_command(mbx_system.internals)
    bot.tree.add_command(mbx_system.archive)
    bot.tree.add_command(mbx_system.unarchive)
    bot.tree.add_command(mbx_system.clone)
    bot.tree.add_command(mbx_system.rules)
    bot.tree.add_command(mbx_system.safety_panel)
    bot.tree.add_command(mbx_system.access)
    bot.tree.add_command(mbx_system.lockdown)
    bot.tree.add_command(mbx_system.unlockdown)
    bot.tree.add_command(mbx_system.status_cmd)
    bot.add_command(mbx_system.sync)

    # Global app-command error handler
    bot.tree.on_error = mbx_system.on_app_command_error
