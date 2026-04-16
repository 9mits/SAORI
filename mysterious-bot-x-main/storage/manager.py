"""
storage/manager.py — multi-guild data manager + context-variable proxy.

Architecture
============
* GuildDataManager  owns a dict[guild_id -> GuildStore]; bot holds one instance.
* ContextVar(_current_store) tracks which GuildStore is "active" for the
  current asyncio task (i.e. the guild that triggered the current interaction
  or event).
* _StoreProxy  is a thin object that reads every attribute from whatever
  GuildStore the ContextVar points to.  bot.data_manager is set to a
  _StoreProxy so that all existing legacy code (bot.data_manager.config,
  bot.data_manager.punishments, etc.) keeps working without modification.

Usage in cog / event handlers
==============================
  token = set_guild_store(bot.guild_manager.get_store(interaction.guild_id))
  try:
      ... legacy code that uses bot.data_manager.config ...
  finally:
      reset_guild_store(token)

DM modmail routing
==================
When a user DMs the bot (no guild context) we look them up in
GuildDataManager.dm_routing: Dict[str, int]  (user_id -> guild_id).
This mapping is rebuilt from all loaded modmail stores on startup and
updated whenever a ticket is opened or closed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional, Tuple

import discord

from .guild_store import GuildStore, _write_json_sync

logger = logging.getLogger("MGXBot.storage")

# ---------------------------------------------------------------------------
# Context variable — one per asyncio task
# ---------------------------------------------------------------------------

_current_store: ContextVar[Optional[GuildStore]] = ContextVar(
    "_current_store", default=None
)


def set_guild_store(store: GuildStore) -> Token:
    """Activate *store* for the current task.  Returns a token to reset later."""
    return _current_store.set(store)


def reset_guild_store(token: Token) -> None:
    """Deactivate the store set by :func:`set_guild_store`."""
    _current_store.reset(token)


def get_guild_store() -> Optional[GuildStore]:
    """Return the currently active GuildStore (may be None)."""
    return _current_store.get()


# ---------------------------------------------------------------------------
# _StoreProxy — transparent proxy over the current GuildStore
# ---------------------------------------------------------------------------

class _StoreProxy:
    """
    Drop-in replacement for the old single-guild DataManager.

    Every attribute lookup is forwarded to the GuildStore that the
    ContextVar currently points at, so all legacy code that reads
    ``bot.data_manager.config`` or calls ``bot.data_manager.add_punishment(…)``
    continues to work without modification.
    """

    __slots__ = ("_manager",)

    def __init__(self, manager: "GuildDataManager") -> None:
        object.__setattr__(self, "_manager", manager)

    def _store(self) -> GuildStore:
        store = _current_store.get()
        if store is not None:
            return store
        # Fallback: return first loaded store (handles rare edge-cases in
        # background tasks that haven't set a guild context explicitly).
        manager: GuildDataManager = object.__getattribute__(self, "_manager")
        if manager._stores:
            return next(iter(manager._stores.values()))
        raise RuntimeError(
            "bot.data_manager accessed with no guild context set. "
            "Ensure set_guild_store() is called before touching data."
        )

    # ── forwarded property shortcuts (avoids __getattr__ on every hot path) ──

    @property
    def config(self) -> dict:
        return self._store().config

    @property
    def punishments(self) -> dict:
        return self._store().punishments

    @property
    def roles(self) -> dict:
        return self._store().roles

    @property
    def mod_stats(self) -> dict:
        return self._store().mod_stats

    @property
    def modmail(self) -> dict:
        return self._store().modmail

    @property
    def modmail_threads(self) -> Dict[int, str]:
        return self._store().modmail_threads

    @property
    def pings(self) -> dict:
        return self._store().pings

    @property
    def lockdown(self) -> dict:
        return self._store().lockdown

    @property
    def message_cache(self):
        return self._store().message_cache

    @property
    def message_cache_index(self) -> dict:
        return self._store().message_cache_index

    @property
    def message_cache_retention_days(self) -> int:
        return self._store().message_cache_retention_days

    @property
    def case_index(self) -> dict:
        return self._store().case_index

    def __getattr__(self, name: str):
        return getattr(self._store(), name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_manager":
            object.__setattr__(self, name, value)
        else:
            setattr(self._store(), name, value)


# ---------------------------------------------------------------------------
# GuildDataManager
# ---------------------------------------------------------------------------

class GuildDataManager:
    """
    Owns all GuildStore instances.  bot.guild_manager is set to one of these.

    The bot also exposes bot.data_manager = _StoreProxy(self) so that
    legacy code can access ``bot.data_manager.config`` transparently.
    """

    def __init__(self, bot, base_dir: Path) -> None:
        self.bot = bot
        self.base_dir: Path = base_dir
        self._stores: Dict[int, GuildStore] = {}
        self._lock = asyncio.Lock()

        # user_id (str) -> guild_id (int) — for DM modmail routing
        self.dm_routing: Dict[str, int] = {}

        # Path for DM routing persistence
        self._dm_routing_path: Path = base_dir / "dm_routing.json"

    # ── store access ──────────────────────────────────────────────────────

    def get_store(self, guild_id: int) -> GuildStore:
        """Return the GuildStore for *guild_id*, creating it if necessary."""
        if guild_id not in self._stores:
            self._stores[guild_id] = GuildStore(guild_id, self.base_dir)
        return self._stores[guild_id]

    async def load_guild(self, guild_id: int) -> GuildStore:
        """Load (or reload) the data for a single guild from disk."""
        store = self.get_store(guild_id)
        await store.load_all()
        self._rebuild_dm_routing_for_guild(guild_id, store)
        return store

    async def load_all_guilds(self) -> None:
        """Load data for every guild the bot is currently in."""
        for guild in self.bot.guilds:
            try:
                await self.load_guild(guild.id)
            except Exception as exc:
                logger.error("Failed to load data for guild %s: %s", guild.id, exc)
        self._save_dm_routing_sync()

    async def save_all(self, force: bool = False) -> None:
        """Flush all dirty GuildStores to disk."""
        for store in list(self._stores.values()):
            try:
                await store.save_all(force=force)
            except Exception as exc:
                logger.error("Failed to save store for guild %s: %s", store.guild_id, exc)

    def iter_stores(self) -> Generator[GuildStore, None, None]:
        """Iterate over all currently-loaded GuildStores."""
        yield from list(self._stores.values())

    @property
    def loaded_guild_ids(self) -> List[int]:
        return list(self._stores.keys())

    # ── DM modmail routing ─────────────────────────────────────────────────

    def _rebuild_dm_routing_for_guild(self, guild_id: int, store: GuildStore) -> None:
        for user_id, ticket in store.modmail.items():
            if isinstance(ticket, dict) and ticket.get("status") == "open":
                self.dm_routing[str(user_id)] = guild_id

    def get_dm_guild_id(self, user_id: str) -> Optional[int]:
        """Return the guild_id where *user_id* has an open modmail ticket."""
        return self.dm_routing.get(str(user_id))

    def register_dm_ticket(self, user_id: str, guild_id: int) -> None:
        self.dm_routing[str(user_id)] = guild_id
        self._save_dm_routing_sync()

    def unregister_dm_ticket(self, user_id: str) -> None:
        self.dm_routing.pop(str(user_id), None)
        self._save_dm_routing_sync()

    def _save_dm_routing_sync(self) -> None:
        try:
            _write_json_sync(self._dm_routing_path, self.dm_routing)
        except Exception as exc:
            logger.warning("Failed to save dm_routing: %s", exc)

    def _load_dm_routing(self) -> None:
        if self._dm_routing_path.exists():
            try:
                with self._dm_routing_path.open("r", encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    self.dm_routing = {str(k): int(v) for k, v in raw.items() if str(v).isdigit()}
            except Exception as exc:
                logger.warning("Failed to load dm_routing: %s", exc)

    # ── database migration (flat-file → per-guild dirs) ───────────────────

    async def migrate_legacy_flat_files(self) -> None:
        """
        One-time migration: if old-style flat database files exist under
        <base_dir>/ (i.e. before multi-guild support), move them into
        <base_dir>/guilds/<guild_id>/ based on the guild_id in config.json.
        """
        legacy_config_path = self.base_dir / "config.json"
        guilds_dir = self.base_dir / "guilds"

        if not legacy_config_path.exists():
            return

        # Only migrate if the guilds dir doesn't already have any data
        if guilds_dir.exists() and any(guilds_dir.iterdir()):
            # Migration already done; remove legacy files silently
            self._cleanup_legacy_files()
            return

        logger.info("Migrating legacy flat-file database to per-guild structure…")

        try:
            with legacy_config_path.open("r", encoding="utf-8") as fh:
                legacy_config = json.load(fh)
        except Exception as exc:
            logger.error("Could not read legacy config.json during migration: %s", exc)
            return

        from modules.mbx_constants import DEFAULT_GUILD_ID
        guild_id = int(legacy_config.get("guild_id", DEFAULT_GUILD_ID))
        dest_dir = guilds_dir / str(guild_id)
        dest_dir.mkdir(parents=True, exist_ok=True)

        legacy_files = {
            "config.json":        "config.json",
            "punishments.json":   "punishments.json",
            "modmail.json":       "modmail.json",
            "roles.json":         "roles.json",
            "mod_stats.json":     "mod_stats.json",
            "message_cache.json": "message_cache.json",
            "pings.json":         "pings.json",
            "lockdown.json":      "lockdown.json",
        }

        for src_name, dst_name in legacy_files.items():
            src = self.base_dir / src_name
            dst = dest_dir / dst_name
            if src.exists() and not dst.exists():
                shutil.copy2(src, dst)
                logger.info("Migrated %s → guilds/%s/%s", src_name, guild_id, dst_name)

        self._cleanup_legacy_files()
        logger.info("Legacy flat-file migration complete for guild %s.", guild_id)

    def _cleanup_legacy_files(self) -> None:
        legacy_names = {
            "config.json", "punishments.json", "modmail.json",
            "roles.json", "mod_stats.json", "message_cache.json",
            "pings.json", "lockdown.json",
        }
        for name in legacy_names:
            path = self.base_dir / name
            if path.exists():
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ── startup ────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Call once in bot.setup_hook, before loading extensions."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._load_dm_routing()
        await self.migrate_legacy_flat_files()
        # Guilds aren't available yet at this point; load them in on_ready
        # via load_all_guilds().

    # ── guild join/leave hooks ─────────────────────────────────────────────

    async def on_guild_join(self, guild: discord.Guild) -> None:
        """Called when the bot joins a new guild."""
        await self.load_guild(guild.id)
        logger.info("Joined guild %s (%s); data store initialised.", guild.name, guild.id)

    def on_guild_remove(self, guild: discord.Guild) -> None:
        """Called when the bot is removed from a guild (data is preserved on disk)."""
        logger.info("Left guild %s (%s); store kept on disk.", guild.name, guild.id)
        # We deliberately keep the store in memory and on disk so data isn't
        # lost if the bot is temporarily kicked and re-invited.
