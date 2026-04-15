from __future__ import annotations

from modules import mbx_moderation


async def setup(bot) -> None:
    bot.tree.add_command(mbx_moderation.ModGroup())
    bot.tree.add_command(mbx_moderation.punish_context)
    bot.tree.add_command(mbx_moderation.history_context)
    bot.add_listener(mbx_moderation.on_raw_reaction_add)
