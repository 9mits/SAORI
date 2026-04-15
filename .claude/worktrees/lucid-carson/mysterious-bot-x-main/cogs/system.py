from __future__ import annotations

from modules import mbx_system


async def setup(bot) -> None:
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
    bot.add_listener(mbx_system.on_guild_role_update)
    bot.add_listener(mbx_system.on_member_update)
    bot.add_listener(mbx_system.on_message)
    bot.add_listener(mbx_system.on_ready)
    bot.tree.on_error = mbx_system.on_app_command_error
