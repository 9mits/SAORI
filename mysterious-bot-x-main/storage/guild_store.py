"""
storage/guild_store.py — per-guild persistent data store.

Each Discord server the bot is in gets one GuildStore instance that owns
all JSON files for that server under  database/guilds/{guild_id}/.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import discord

from modules.mbx_constants import (
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_ARCHIVE_CAT_ID,
    DEFAULT_MAX_UNREAD_PINGS,
    DEFAULT_MESSAGE_CACHE_LIMIT,
    DEFAULT_MESSAGE_CACHE_RETENTION_DAYS,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    DEFAULT_SPAM_ROLE_ID,
)
from modules.mbx_services import (
    DEFAULT_CANNED_REPLIES,
    DEFAULT_NATIVE_AUTOMOD_SETTINGS,
    DEFAULT_SCHEMA_VERSION,
    normalize_case_record,
    run_schema_migrations,
)

logger = logging.getLogger("MGXBot.storage")


# ---------------------------------------------------------------------------
# Low-level I/O helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path, exc)
    return default


def _write_json_sync(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp.write("\n")
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


async def _write_json(path: Path, data: Any) -> None:
    await asyncio.to_thread(_write_json_sync, path, data)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm_int(value: Any, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = default
    if maximum is not None:
        n = min(n, maximum)
    return max(minimum, n)


# ---------------------------------------------------------------------------
# GuildStore
# ---------------------------------------------------------------------------

class GuildStore:
    """
    All persistent data for a single Discord server.

    Files are stored under  <base_dir>/guilds/<guild_id>/
    """

    # File names inside the guild directory
    _FILES = {
        "config":        "config.json",
        "roles":         "roles.json",
        "punishments":   "punishments.json",
        "mod_stats":     "mod_stats.json",
        "message_cache": "message_cache.json",
        "pings":         "pings.json",
        "lockdown":      "lockdown.json",
        "modmail":       "modmail.json",
    }

    def __init__(self, guild_id: int, base_dir: Path) -> None:
        self.guild_id: int = guild_id
        self._dir: Path = base_dir / "guilds" / str(guild_id)
        self._dir.mkdir(parents=True, exist_ok=True)

        # ── data ──────────────────────────────────────────────────────────
        self.config: dict = {}
        self.roles: dict = {}
        self.punishments: dict = {}
        self.case_index: Dict[int, Tuple[str, dict]] = {}
        self.mod_stats: dict = {}
        self.message_cache: deque = deque(maxlen=DEFAULT_MESSAGE_CACHE_LIMIT)
        self.message_cache_index: Dict[int, dict] = {}
        self.pings: dict = {}
        self.modmail: dict = {}
        self.modmail_threads: Dict[int, str] = {}
        self.lockdown: dict = {}
        self.message_cache_retention_days: int = DEFAULT_MESSAGE_CACHE_RETENTION_DAYS

        # ── dirty flags ───────────────────────────────────────────────────
        self._dirty_config = False
        self._dirty_roles = False
        self._dirty_punishments = False
        self._dirty_stats = False
        self._dirty_message_cache = False
        self._dirty_pings = False
        self._dirty_modmail = False
        self._dirty_lockdown = False
        self._save_lock = asyncio.Lock()

    # ── path helpers ───────────────────────────────────────────────────────

    def _path(self, key: str) -> Path:
        return self._dir / self._FILES[key]

    # ── index builders ────────────────────────────────────────────────────

    def _rebuild_message_cache_index(self) -> None:
        self.message_cache_index = {}
        for record in self.message_cache:
            rid = _parse_int(record.get("id"))
            if rid is not None:
                record["id"] = rid
                self.message_cache_index[rid] = record

    def _rebuild_modmail_index(self) -> None:
        self.modmail_threads = {}
        for user_id, ticket in self.modmail.items():
            tid = _parse_int(ticket.get("thread_id"))
            if tid is not None:
                self.modmail_threads[tid] = user_id

    def _rebuild_case_index(self) -> None:
        self.case_index = {}
        for user_id, records in self.punishments.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if isinstance(record, dict):
                    self._index_case_record(user_id, record)

    def _index_case_record(self, user_id: str, record: dict) -> None:
        case_id = record.get("case_id")
        if isinstance(case_id, int) and case_id > 0:
            self.case_index[case_id] = (user_id, record)

    # ── message cache helpers ──────────────────────────────────────────────

    def _configure_cache_limits(self) -> None:
        limit = _norm_int(
            self.config.get("message_cache_limit", DEFAULT_MESSAGE_CACHE_LIMIT),
            DEFAULT_MESSAGE_CACHE_LIMIT, minimum=100, maximum=50000,
        )
        if self.message_cache.maxlen != limit:
            self.message_cache = deque(list(self.message_cache)[-limit:], maxlen=limit)
        self.message_cache_retention_days = _norm_int(
            self.config.get("message_cache_retention_days", DEFAULT_MESSAGE_CACHE_RETENTION_DAYS),
            DEFAULT_MESSAGE_CACHE_RETENTION_DAYS, minimum=1, maximum=90,
        )
        self._rebuild_message_cache_index()

    def _normalize_message_cache_record(self, record: Any) -> Optional[dict]:
        if not isinstance(record, dict):
            return None
        norm = dict(record)
        rid = _parse_int(norm.get("id"))
        if rid is None:
            return None
        norm["id"] = rid
        aid = _parse_int(norm.get("author_id"))
        if aid is not None:
            norm["author_id"] = aid
        cid = _parse_int(norm.get("channel_id"))
        if cid is not None:
            norm["channel_id"] = cid
        ca = norm.get("created_at")
        if not isinstance(ca, datetime):
            norm["created_at"] = _parse_iso(ca) or discord.utils.utcnow()
        norm["attachments"] = norm.get("attachments", []) if isinstance(norm.get("attachments"), list) else []
        norm["stickers"] = norm.get("stickers", []) if isinstance(norm.get("stickers"), list) else []
        norm["deleted"] = bool(norm.get("deleted", False))
        norm["edited"] = bool(norm.get("edited", False))
        return norm

    def _prune_message_cache(self) -> None:
        cutoff = discord.utils.utcnow() - timedelta(days=self.message_cache_retention_days)
        pruned = False
        while self.message_cache:
            oldest = self.message_cache[0]
            ca = oldest.get("created_at")
            if not isinstance(ca, datetime):
                ca = _parse_iso(ca) or discord.utils.utcnow()
                oldest["created_at"] = ca
            if ca >= cutoff:
                break
            removed = self.message_cache.popleft()
            self.message_cache_index.pop(removed.get("id"), None)
            pruned = True
        if pruned:
            self._dirty_message_cache = True

    def _append_message_record(self, record: dict, *, mark_dirty: bool = True) -> None:
        norm = self._normalize_message_cache_record(record)
        if norm is None:
            if mark_dirty:
                self._dirty_message_cache = True
            return
        if len(self.message_cache) >= self.message_cache.maxlen:
            removed = self.message_cache.popleft()
            self.message_cache_index.pop(removed.get("id"), None)
        self.message_cache.append(norm)
        rid = norm["id"]
        if rid is not None:
            self.message_cache_index[rid] = norm
        self._prune_message_cache()
        if mark_dirty:
            self._dirty_message_cache = True

    def _serialize_message_cache(self) -> List[dict]:
        result = []
        for msg in list(self.message_cache):
            m = msg.copy()
            if isinstance(m.get("created_at"), datetime):
                m["created_at"] = m["created_at"].isoformat()
            result.append(m)
        return result

    # ── normalise punishments ──────────────────────────────────────────────

    def _normalize_punishments(self) -> None:
        if not isinstance(self.punishments, dict):
            self.punishments = {}
            self._dirty_punishments = True
            return

        highest = _norm_int(self.config.get("case_counter", 0), 0, minimum=0)
        changed = False
        now = discord.utils.utcnow()

        for uid, records in list(self.punishments.items()):
            if not isinstance(records, list):
                self.punishments[uid] = []
                changed = True
                continue
            norm_records = []
            for rec in records:
                if not isinstance(rec, dict):
                    changed = True
                    continue
                cid = rec.get("case_id")
                if isinstance(cid, int) and cid > 0:
                    highest = max(highest, cid)
                else:
                    highest += 1
                    rec["case_id"] = highest
                    changed = True
                if rec.get("type") == "ban":
                    dur = rec.get("duration_minutes", 0)
                    if dur == -1:
                        active = True
                    elif dur > 0:
                        issued = _parse_iso(rec.get("timestamp"))
                        active = bool(issued and issued + timedelta(minutes=dur) > now)
                    else:
                        active = False
                    if rec.get("active") != active:
                        rec["active"] = active
                        changed = True
                if normalize_case_record(rec):
                    changed = True
                norm_records.append(rec)
            self.punishments[uid] = norm_records

        if self.config.get("case_counter") != highest:
            self.config["case_counter"] = highest
            self._dirty_config = True
        self._rebuild_case_index()
        if changed:
            self._dirty_punishments = True

    # ── public API: loading ────────────────────────────────────────────────

    async def load_all(self) -> None:
        """Load all data from disk, apply defaults, run migrations."""
        self.config = self._ensure_dict(
            _read_json(self._path("config"), {}), "config.json"
        )
        had_general_log = "general_log_channel_id" in self.config
        legacy_log = self.config.get("log_channel_id")

        defaults: dict = {
            "guild_id": self.guild_id,
            "min_boosts_for_role": 0,
            "whitelist": {},
            "punishment_rules": DEFAULT_RULES,
            "mod_roles": [],
            "stats": {"total_issued": 0, "cases_cleared": 0},
            "locked_channels": {},
            "archived_channels": {},
            "cr_whitelist_users": {},
            "cr_whitelist_roles": {},
            "cr_blacklist_users": [],
            "cr_blacklist_roles": [],
            "security": {"max_actions_per_min": 10},
            "smart_automod": {
                "duplicate_window_seconds": 20,
                "duplicate_threshold": 4,
                "max_caps_ratio": 0.75,
                "caps_min_length": 12,
                "blocked_patterns": [],
                "exempt_channels": [],
                "exempt_roles": [],
            },
            "native_automod": DEFAULT_NATIVE_AUTOMOD_SETTINGS,
            "immunity_list": [],
            "debug": {},
            "token_env_var": "DISCORD_BOT_TOKEN",
            "case_counter": 0,
            "schema_version": DEFAULT_SCHEMA_VERSION,
            "message_cache_limit": DEFAULT_MESSAGE_CACHE_LIMIT,
            "message_cache_retention_days": DEFAULT_MESSAGE_CACHE_RETENTION_DAYS,
            "max_unread_pings_per_user": DEFAULT_MAX_UNREAD_PINGS,
            "feature_flags": {},
            "modmail_canned_replies": DEFAULT_CANNED_REPLIES,
            "modmail_sla_minutes": 60,
            "dm_modmail_panel_cooldown_minutes": 30,
            "escalation_matrix": [],
            "general_log_channel_id": 0,
            "punishment_log_channel_id": 0,
            "automod_log_channel_id": 0,
            "automod_report_channel_id": 0,
            "role_owner": DEFAULT_ROLE_OWNER,
            "role_admin": DEFAULT_ROLE_ADMIN,
            "role_mod": DEFAULT_ROLE_MOD,
            "role_community_manager": DEFAULT_ROLE_COMMUNITY_MANAGER,
            "role_anchor": DEFAULT_ANCHOR_ROLE_ID,
            "category_archive": DEFAULT_ARCHIVE_CAT_ID,
            "role_mention_spam_target": DEFAULT_SPAM_ROLE_ID,
            "brand_name": "",
            "brand_icon_url": "",
            "brand_banner_url": "",
            "brand_color": 0,
        }
        for key, val in defaults.items():
            if key not in self.config:
                self.config[key] = copy.deepcopy(val)
                self._dirty_config = True

        # Always ensure guild_id matches
        if self.config.get("guild_id") != self.guild_id:
            self.config["guild_id"] = self.guild_id
            self._dirty_config = True

        if not had_general_log and legacy_log:
            self.config["general_log_channel_id"] = legacy_log
            self._dirty_config = True

        self._configure_cache_limits()

        self.roles = self._ensure_dict(
            _read_json(self._path("roles"), {}), "roles.json"
        )
        self.punishments = self._ensure_dict(
            _read_json(self._path("punishments"), {}), "punishments.json"
        )
        self._normalize_punishments()

        self.mod_stats = self._ensure_dict(
            _read_json(self._path("mod_stats"), {}), "mod_stats.json"
        )
        self.pings = self._ensure_dict(
            _read_json(self._path("pings"), {}), "pings.json"
        )
        self.modmail = self._ensure_dict(
            _read_json(self._path("modmail"), {}), "modmail.json"
        )

        migrated, notes = run_schema_migrations(self.config, self.punishments, self.modmail)
        if migrated:
            self._dirty_config = True
            self._dirty_punishments = True
            self._dirty_modmail = True
            for note in notes:
                logger.info("[guild:%s] Migration: %s", self.guild_id, note)

        self.lockdown = self._ensure_dict(
            _read_json(self._path("lockdown"), {}), "lockdown.json"
        )
        self._rebuild_case_index()
        self._rebuild_modmail_index()

        # message cache
        self.message_cache.clear()
        self.message_cache_index.clear()
        raw = self._ensure_list(
            _read_json(self._path("message_cache"), []), "message_cache.json"
        )
        for msg in raw:
            norm = self._normalize_message_cache_record(msg)
            if norm is None:
                self._dirty_message_cache = True
                continue
            self._append_message_record(norm, mark_dirty=False)
        self._prune_message_cache()

    # ── public API: saving ────────────────────────────────────────────────

    async def save_all(self, force: bool = False) -> None:
        async with self._save_lock:
            if self._dirty_config or force:
                await _write_json(self._path("config"), self.config)
                self._dirty_config = False
            if self._dirty_roles or force:
                await _write_json(self._path("roles"), self.roles)
                self._dirty_roles = False
            if self._dirty_punishments or force:
                self._rebuild_case_index()
                await _write_json(self._path("punishments"), self.punishments)
                self._dirty_punishments = False
            if self._dirty_stats or force:
                await _write_json(self._path("mod_stats"), self.mod_stats)
                self._dirty_stats = False
            if self._dirty_message_cache or force:
                self._prune_message_cache()
                await _write_json(self._path("message_cache"), self._serialize_message_cache())
                self._dirty_message_cache = False
            if self._dirty_pings or force:
                await _write_json(self._path("pings"), self.pings)
                self._dirty_pings = False
            if self._dirty_modmail or force:
                self._rebuild_modmail_index()
                await _write_json(self._path("modmail"), self.modmail)
                self._dirty_modmail = False
            if self._dirty_lockdown or force:
                await _write_json(self._path("lockdown"), self.lockdown)
                self._dirty_lockdown = False

    def mark_config_dirty(self) -> None:
        self._dirty_config = True

    async def save_config(self) -> None:
        self.mark_config_dirty()
        await self.save_all()

    async def save_roles(self) -> None:
        self._dirty_roles = True
        await self.save_all()

    async def save_punishments(self) -> None:
        self._dirty_punishments = True
        await self.save_all()

    async def save_mod_stats(self) -> None:
        self._dirty_stats = True
        await self.save_all()

    async def save_lockdown(self) -> None:
        self._dirty_lockdown = True
        await self.save_all()

    async def save_modmail(self) -> None:
        self._dirty_modmail = True
        await self.save_all()

    async def save_message_cache(self) -> None:
        self._dirty_message_cache = True
        await self.save_all()

    # ── public API: data operations ───────────────────────────────────────

    def allocate_case_id(self) -> int:
        current = _norm_int(self.config.get("case_counter", 0), 0, minimum=0)
        next_id = current + 1
        self.config["case_counter"] = next_id
        self._dirty_config = True
        return next_id

    def prepare_punishment_record(self, record: dict) -> dict:
        from modules.mbx_utils import now_iso
        prep = dict(record)
        cid = prep.get("case_id")
        if not isinstance(cid, int) or cid <= 0:
            prep["case_id"] = self.allocate_case_id()
        if "timestamp" not in prep:
            prep["timestamp"] = now_iso()
        if "active" not in prep:
            prep["active"] = prep.get("type") == "ban"
        normalize_case_record(prep)
        return prep

    async def add_punishment(self, uid: str, record: dict, *, persist: bool = True) -> dict:
        if uid not in self.punishments:
            self.punishments[uid] = []
        prepared = self.prepare_punishment_record(record)
        self.punishments[uid].append(prepared)
        self._index_case_record(uid, prepared)
        self._dirty_punishments = True
        if persist:
            await self.save_all()
        return prepared

    def get_case(self, case_id: int) -> Tuple[Optional[str], Optional[dict]]:
        nid = _parse_int(case_id)
        if nid is None:
            return None, None
        entry = self.case_index.get(nid)
        if entry is not None:
            user_id, record = entry
            if record in self.punishments.get(user_id, []):
                return entry
            self.case_index.pop(nid, None)
        self._rebuild_case_index()
        return self.case_index.get(nid, (None, None))

    def get_user_cases(self, user_id: int) -> List[dict]:
        records = self.punishments.get(str(user_id), [])
        return sorted(
            [r for r in records if isinstance(r, dict)],
            key=lambda r: r.get("case_id", 0),
            reverse=True,
        )

    def get_modmail_user_id(self, thread_id: int) -> Optional[str]:
        return self.modmail_threads.get(thread_id)

    # ── message cache ops ─────────────────────────────────────────────────

    def cache_message(self, record: dict) -> None:
        self._append_message_record(record)

    def get_cached_message(self, message_id: int) -> Optional[dict]:
        return self.message_cache_index.get(message_id)

    def mark_message_deleted(self, message_id: int) -> bool:
        record = self.get_cached_message(message_id)
        if not record:
            return False
        record["deleted"] = True
        self._dirty_message_cache = True
        return True

    def update_cached_message(self, message_id: int, **changes) -> bool:
        record = self.get_cached_message(message_id)
        if not record:
            return False
        record.update(changes)
        self._dirty_message_cache = True
        return True

    # ── helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _ensure_dict(value: Any, label: str) -> dict:
        if isinstance(value, dict):
            return value
        logger.warning("Expected %s to contain a JSON object; resetting.", label)
        return {}

    @staticmethod
    def _ensure_list(value: Any, label: str) -> list:
        if isinstance(value, list):
            return value
        logger.warning("Expected %s to contain a JSON array; resetting.", label)
        return []
