"""
storage — per-guild persistent data layer for Mysterious Bot X.

Public surface:
  GuildStore          One guild's complete data (config, punishments, modmail…)
  GuildDataManager    Owns all GuildStore instances; acts as context proxy.
  set_guild_store     Set the context-var for the current async task.
  reset_guild_store   Reset after the command/event finishes.
  get_guild_store     Read the context-var (may return None).
"""

from .guild_store import GuildStore
from .manager import GuildDataManager, get_guild_store, reset_guild_store, set_guild_store

__all__ = [
    "GuildStore",
    "GuildDataManager",
    "get_guild_store",
    "reset_guild_store",
    "set_guild_store",
]
