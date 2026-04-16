"""
cogs/roles.py — RolesCog

Registers /role, /role manage, /role settings, and /help commands.
No event listeners needed; guild context is set via the tree-command wrapper
installed in setup_hook.
"""
from __future__ import annotations

from discord.ext import commands

from modules import mbx_roles


class RolesCog(commands.Cog, name="Roles"):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot: commands.Bot) -> None:
    cog = RolesCog(bot)
    await bot.add_cog(cog)

    bot.tree.add_command(mbx_roles.role_cmd)
    bot.tree.add_command(mbx_roles.role_manage)
    bot.tree.add_command(mbx_roles.role_settings)
    bot.tree.add_command(mbx_roles.help_cmd)
