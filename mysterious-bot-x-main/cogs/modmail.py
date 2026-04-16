"""
cogs/modmail.py — ModmailCog

The modmail system is DM-driven.  All DM message handling is done in the
SystemCog.on_message listener (which already sets guild context via the
dm_routing table).  This cog registers itself so the extension loader
succeeds and provides a hook point for future modmail-specific events.
"""
from __future__ import annotations

from discord.ext import commands


class ModmailCog(commands.Cog, name="Modmail"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModmailCog(bot))
