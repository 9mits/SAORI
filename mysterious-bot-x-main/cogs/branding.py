"""
cogs/branding.py — BrandingCog

Registers the /branding command group for per-guild custom branding.
"""
from __future__ import annotations

from discord.ext import commands

from modules import mbx_branding


class BrandingCog(commands.Cog, name="Branding"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BrandingCog(bot))
    bot.tree.add_command(mbx_branding.BrandingGroup())
