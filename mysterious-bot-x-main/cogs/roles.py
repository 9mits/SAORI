from __future__ import annotations

from modules import mbx_roles


async def setup(bot) -> None:
    bot.tree.add_command(mbx_roles.role_cmd)
    bot.tree.add_command(mbx_roles.role_manage)
    bot.tree.add_command(mbx_roles.role_settings)
    bot.tree.add_command(mbx_roles.help_cmd)
