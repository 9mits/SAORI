from __future__ import annotations

from modules import mbx_automod


async def setup(bot) -> None:
    bot.tree.add_command(mbx_automod.automod_cmd)
    bot.add_listener(mbx_automod.on_automod_action)
    bot.add_listener(mbx_automod.on_socket_raw_receive)
