"""
mbx_data.py — DataManager, AntiAbuseSystem, path constants, and low-level I/O helpers.
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
    DEFAULT_GUILD_ID,
    DEFAULT_MAX_UNREAD_PINGS,
    DEFAULT_MESSAGE_CACHE_LIMIT,
    DEFAULT_MESSAGE_CACHE_RETENTION_DAYS,
    DEFAULT_ROLE_ADMIN,
    DEFAULT_ANCHOR_ROLE_ID,
    DEFAULT_ROLE_COMMUNITY_MANAGER,
    DEFAULT_ROLE_MOD,
    DEFAULT_ROLE_OWNER,
    DEFAULT_RULES,
    DEFAULT_SPAM_ROLE_ID,
    DEFAULT_ARCHIVE_CAT_ID,
    TOKEN_ENV_VARS,
)
from modules.mbx_services import (
    DEFAULT_CANNED_REPLIES,
    DEFAULT_NATIVE_AUTOMOD_SETTINGS,
    DEFAULT_SCHEMA_VERSION,
    normalize_case_record,
    run_schema_migrations,
    ticket_needs_sla_alert,
)

logger = logging.getLogger("MGXBot")

# ----------------- PATHS -----------------
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"
ROLES_FILE = DB_DIR / "roles.json"
CONFIG_FILE = DB_DIR / "config.json"
PUNISHMENTS_FILE = DB_DIR / "punishments.json"
MOD_STATS_FILE = DB_DIR / "mod_stats.json"
MESSAGE_CACHE_FILE = DB_DIR / "message_cache.json"
PINGS_FILE = DB_DIR / "pings.json"
LOCKDOWN_FILE = DB_DIR / "lockdown.json"
MODMAIL_FILE = DB_DIR / "modmail.json"
# -----------------------------------------


def read_json_file(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path.name, exc)
    return default


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def resolve_bot_token() -> str:
    bootstrap_config = read_json_file(CONFIG_FILE, {})
    env_var_order: List[str] = []

    configured_env_var = bootstrap_config.get("token_env_var")
    if isinstance(configured_env_var, str) and configured_env_var.strip():
        env_var_order.append(configured_env_var.strip())

    for env_var in TOKEN_ENV_VARS:
        if env_var not in env_var_order:
            env_var_order.append(env_var)

    for env_var in env_var_order:
        token = os.getenv(env_var)
        if token:
            return token.strip()

    raise RuntimeError(
        "Discord bot token is not configured. Set one of the supported environment variables "
        f"({', '.join(env_var_order)})."
    )


# ----------------- Storage helpers -----------------
class DataManager:
    def __init__(self, bot):
        self.bot = bot
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

        self._dirty_config = False
        self._dirty_roles = False
        self._dirty_punishments = False
        self._dirty_stats = False
        self._dirty_message_cache = False
        self._dirty_pings = False
        self._dirty_modmail = False
        self._dirty_lockdown = False
        self._save_lock = asyncio.Lock()

    def _load_json(self, path, default):
        return read_json_file(Path(path), default)

    def _save_json_sync(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
            ) as temp_file:
                json.dump(data, temp_file, indent=2, ensure_ascii=False)
                temp_file.write("\n")
                temp_name = temp_file.name
            os.replace(temp_name, path)
        finally:
            if temp_name and os.path.exists(temp_name):
                try:
                    os.remove(temp_name)
                except OSError:
                    pass

    async def _save_json(self, path, data):
        await asyncio.to_thread(self._save_json_sync, path, data)

    def _normalize_positive_int(self, value: Any, default: int, *, minimum: int = 1, maximum: Optional[int] = None) -> int:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            normalized = default
        if maximum is not None:
            normalized = min(normalized, maximum)
        return max(minimum, normalized)

    def _configure_cache_limits(self):
        cache_limit = self._normalize_positive_int(
            self.config.get("message_cache_limit", DEFAULT_MESSAGE_CACHE_LIMIT),
            DEFAULT_MESSAGE_CACHE_LIMIT,
            minimum=100,
            maximum=50000,
        )
        if self.message_cache.maxlen != cache_limit:
            self.message_cache = deque(list(self.message_cache)[-cache_limit:], maxlen=cache_limit)
        self.message_cache_retention_days = self._normalize_positive_int(
            self.config.get("message_cache_retention_days", DEFAULT_MESSAGE_CACHE_RETENTION_DAYS),
            DEFAULT_MESSAGE_CACHE_RETENTION_DAYS,
            minimum=1,
            maximum=90,
        )
        self._rebuild_message_cache_index()

    def _parse_optional_int(self, value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_message_cache_record(self, record: Any) -> Optional[dict]:
        if not isinstance(record, dict):
            return None
        normalized = dict(record)
        message_id = self._parse_optional_int(normalized.get("id"))
        if message_id is None:
            return None
        normalized["id"] = message_id
        author_id = self._parse_optional_int(normalized.get("author_id"))
        if author_id is not None:
            normalized["author_id"] = author_id
        channel_id = self._parse_optional_int(normalized.get("channel_id"))
        if channel_id is not None:
            normalized["channel_id"] = channel_id
        created_at = normalized.get("created_at")
        if not isinstance(created_at, datetime):
            normalized["created_at"] = parse_iso_datetime(created_at) or discord.utils.utcnow()
        attachments = normalized.get("attachments", [])
        normalized["attachments"] = attachments if isinstance(attachments, list) else []
        stickers = normalized.get("stickers", [])
        normalized["stickers"] = stickers if isinstance(stickers, list) else []
        normalized["deleted"] = bool(normalized.get("deleted", False))
        normalized["edited"] = bool(normalized.get("edited", False))
        return normalized

    def _rebuild_message_cache_index(self):
        self.message_cache_index = {}
        for record in self.message_cache:
            record_id = self._parse_optional_int(record.get("id"))
            if record_id is not None:
                record["id"] = record_id
                self.message_cache_index[record_id] = record

    def _rebuild_modmail_index(self):
        self.modmail_threads = {}
        for user_id, ticket in self.modmail.items():
            thread_id = self._parse_optional_int(ticket.get("thread_id"))
            if thread_id is not None:
                self.modmail_threads[thread_id] = user_id

    def _rebuild_case_index(self):
        self.case_index = {}
        for user_id, records in self.punishments.items():
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                self._index_case_record(user_id, record)

    def _index_case_record(self, user_id: str, record: dict):
        case_id = record.get("case_id")
        if isinstance(case_id, int) and case_id > 0:
            self.case_index[case_id] = (user_id, record)

    def _prune_message_cache(self):
        cutoff = discord.utils.utcnow() - timedelta(days=self.message_cache_retention_days)
        pruned = False
        while self.message_cache:
            oldest = self.message_cache[0]
            created_at = oldest.get("created_at")
            if not isinstance(created_at, datetime):
                created_at = parse_iso_datetime(created_at) or discord.utils.utcnow()
                oldest["created_at"] = created_at
            if created_at >= cutoff:
                break
            removed = self.message_cache.popleft()
            self.message_cache_index.pop(removed.get("id"), None)
            pruned = True
        if pruned:
            self._dirty_message_cache = True

    def _append_message_record(self, record: dict, *, mark_dirty: bool = True):
        normalized = self._normalize_message_cache_record(record)
        if normalized is None:
            if mark_dirty:
                self._dirty_message_cache = True
            return
        if len(self.message_cache) >= self.message_cache.maxlen:
            removed = self.message_cache.popleft()
            self.message_cache_index.pop(removed.get("id"), None)
        self.message_cache.append(normalized)
        record_id = normalized["id"]
        if record_id is not None:
            self.message_cache_index[record_id] = normalized
        self._prune_message_cache()
        if mark_dirty:
            self._dirty_message_cache = True

    def _serialize_message_cache(self) -> List[dict]:
        serializable = []
        for msg in list(self.message_cache):
            msg_copy = msg.copy()
            if isinstance(msg_copy.get("created_at"), datetime):
                msg_copy["created_at"] = msg_copy["created_at"].isoformat()
            serializable.append(msg_copy)
        return serializable

    def _ensure_dict(self, value: Any, path: Path) -> dict:
        if isinstance(value, dict):
            return value
        logger.warning("Expected %s to contain a JSON object. Resetting to defaults.", path.name)
        return {}

    def _ensure_list(self, value: Any, path: Path) -> list:
        if isinstance(value, list):
            return value
        logger.warning("Expected %s to contain a JSON array. Resetting to defaults.", path.name)
        return []

    async def load_all(self):
        self.config = self._ensure_dict(self._load_json(CONFIG_FILE, {}), CONFIG_FILE)
        had_general_log_channel = "general_log_channel_id" in self.config
        legacy_log_channel_id = self.config.get("log_channel_id")

        defaults = {
            "min_boosts_for_role": 0, "whitelist": {}, "punishment_rules": DEFAULT_RULES,
            "mod_roles": [], "stats": {"total_issued": 0, "cases_cleared": 0},
            "locked_channels": {}, "archived_channels": {},
            "cr_whitelist_users": {}, "cr_whitelist_roles": {}, "cr_blacklist_users": [], "cr_blacklist_roles": [],
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
            "immunity_list": [], "debug": {},
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
            "guild_id": DEFAULT_GUILD_ID,
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
        }
        for k, v in defaults.items():
            if k not in self.config:
                self.config[k] = copy.deepcopy(v)
                self._dirty_config = True

        if not had_general_log_channel and legacy_log_channel_id:
            self.config["general_log_channel_id"] = legacy_log_channel_id
            self._dirty_config = True

        self._configure_cache_limits()
        self.roles = self._ensure_dict(self._load_json(ROLES_FILE, {}), ROLES_FILE)
        self.punishments = self._ensure_dict(self._load_json(PUNISHMENTS_FILE, {}), PUNISHMENTS_FILE)
        self._normalize_punishments()
        self.mod_stats = self._ensure_dict(self._load_json(MOD_STATS_FILE, {}), MOD_STATS_FILE)
        self.pings = self._ensure_dict(self._load_json(PINGS_FILE, {}), PINGS_FILE)
        self.modmail = self._ensure_dict(self._load_json(MODMAIL_FILE, {}), MODMAIL_FILE)
        migrated, migration_notes = run_schema_migrations(self.config, self.punishments, self.modmail)
        if migrated:
            self._dirty_config = True
            self._dirty_punishments = True
            self._dirty_modmail = True
            for note in migration_notes:
                logger.info("Migration: %s", note)
        self.lockdown = self._ensure_dict(self._load_json(LOCKDOWN_FILE, {}), LOCKDOWN_FILE)
        self._rebuild_case_index()
        self._rebuild_modmail_index()

        self.message_cache.clear()
        self.message_cache_index.clear()
        raw_cache = self._ensure_list(self._load_json(MESSAGE_CACHE_FILE, []), MESSAGE_CACHE_FILE)
        for msg in raw_cache:
            normalized = self._normalize_message_cache_record(msg)
            if normalized is None:
                self._dirty_message_cache = True
                continue
            self._append_message_record(normalized, mark_dirty=False)
        self._prune_message_cache()

    async def save_all(self, force=False):
        async with self._save_lock:
            if self._dirty_config or force:
                await self._save_json(CONFIG_FILE, self.config)
                self._dirty_config = False
            if self._dirty_roles or force:
                await self._save_json(ROLES_FILE, self.roles)
                self._dirty_roles = False
            if self._dirty_punishments or force:
                self._rebuild_case_index()
                await self._save_json(PUNISHMENTS_FILE, self.punishments)
                self._dirty_punishments = False
            if self._dirty_stats or force:
                await self._save_json(MOD_STATS_FILE, self.mod_stats)
                self._dirty_stats = False
            if self._dirty_message_cache or force:
                self._prune_message_cache()
                await self._save_json(MESSAGE_CACHE_FILE, self._serialize_message_cache())
                self._dirty_message_cache = False
            if self._dirty_pings or force:
                await self._save_json(PINGS_FILE, self.pings)
                self._dirty_pings = False
            if self._dirty_modmail or force:
                self._rebuild_modmail_index()
                await self._save_json(MODMAIL_FILE, self.modmail)
                self._dirty_modmail = False
            if self._dirty_lockdown or force:
                await self._save_json(LOCKDOWN_FILE, self.lockdown)
                self._dirty_lockdown = False

    async def save_message_cache(self):
        self._dirty_message_cache = True
        await self.save_all()

    def mark_config_dirty(self):
        self._dirty_config = True

    async def save_config(self):
        self.mark_config_dirty()
        await self.save_all()

    async def save_roles(self):
        self._dirty_roles = True
        await self.save_all()

    async def save_punishments(self):
        self._dirty_punishments = True
        await self.save_all()

    async def save_mod_stats(self):
        self._dirty_stats = True
        await self.save_all()

    async def save_lockdown(self):
        self._dirty_lockdown = True
        await self.save_all()

    async def add_punishment(self, uid, record, *, persist: bool = True):
        if uid not in self.punishments:
            self.punishments[uid] = []
        prepared = self.prepare_punishment_record(record)
        self.punishments[uid].append(prepared)
        self._index_case_record(uid, prepared)
        self._dirty_punishments = True
        if persist:
            await self.save_all()
        return prepared

    async def save_modmail(self):
        self._dirty_modmail = True
        await self.save_all()

    def cache_message(self, record: dict):
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

    def get_modmail_user_id(self, thread_id: int) -> Optional[str]:
        return self.modmail_threads.get(thread_id)

    def get_case(self, case_id: int) -> Tuple[Optional[str], Optional[dict]]:
        normalized_case_id = self._parse_optional_int(case_id)
        if normalized_case_id is None:
            return None, None
        entry = self.case_index.get(normalized_case_id)
        if entry is not None:
            user_id, record = entry
            if record in self.punishments.get(user_id, []):
                return entry
            self.case_index.pop(normalized_case_id, None)
        self._rebuild_case_index()
        return self.case_index.get(normalized_case_id, (None, None))

    def get_user_cases(self, user_id: int) -> List[dict]:
        records = self.punishments.get(str(user_id), [])
        return sorted(
            [record for record in records if isinstance(record, dict)],
            key=lambda record: record.get("case_id", 0),
            reverse=True,
        )

    def allocate_case_id(self) -> int:
        current = self._normalize_positive_int(self.config.get("case_counter", 0), 0, minimum=0)
        next_case_id = current + 1
        self.config["case_counter"] = next_case_id
        self._dirty_config = True
        return next_case_id

    def prepare_punishment_record(self, record: dict) -> dict:
        from modules.mbx_utils import now_iso
        prepared = dict(record)
        case_id = prepared.get("case_id")
        if not isinstance(case_id, int) or case_id <= 0:
            prepared["case_id"] = self.allocate_case_id()
        if "timestamp" not in prepared:
            prepared["timestamp"] = now_iso()
        if "active" not in prepared:
            prepared["active"] = prepared.get("type") == "ban"
        normalize_case_record(prepared)
        return prepared

    def _normalize_punishments(self):
        if not isinstance(self.punishments, dict):
            self.punishments = {}
            self._dirty_punishments = True
            return

        highest_case_id = self._normalize_positive_int(self.config.get("case_counter", 0), 0, minimum=0)
        changed = False
        now = discord.utils.utcnow()

        for uid, records in list(self.punishments.items()):
            if not isinstance(records, list):
                self.punishments[uid] = []
                changed = True
                continue

            normalized_records = []
            for record in records:
                if not isinstance(record, dict):
                    changed = True
                    continue

                case_id = record.get("case_id")
                if isinstance(case_id, int) and case_id > 0:
                    highest_case_id = max(highest_case_id, case_id)
                else:
                    highest_case_id += 1
                    record["case_id"] = highest_case_id
                    changed = True

                record_type = record.get("type")
                if record_type == "ban":
                    duration = record.get("duration_minutes", 0)
                    if duration == -1:
                        active = True
                    elif duration > 0:
                        issued_at = parse_iso_datetime(record.get("timestamp"))
                        active = bool(issued_at and issued_at + timedelta(minutes=duration) > now)
                    else:
                        active = False
                    if record.get("active") != active:
                        record["active"] = active
                        changed = True

                if normalize_case_record(record):
                    changed = True

                normalized_records.append(record)

            self.punishments[uid] = normalized_records

        if self.config.get("case_counter") != highest_case_id:
            self.config["case_counter"] = highest_case_id
            self._dirty_config = True

        self._rebuild_case_index()
        if changed:
            self._dirty_punishments = True


# ----------------- Security -----------------
class AntiAbuseSystem:
    def __init__(self):
        self._tracker = defaultdict(lambda: deque(maxlen=15))
        self.cooldowns: Dict[str, float] = {}
        self.mention_spam_tracker = defaultdict(lambda: deque(maxlen=10))
        self.smart_automod_tracker = defaultdict(lambda: deque(maxlen=8))

    def check_rate_limit(self, user_id: int, config: dict) -> bool:
        now = time.time()
        limit = config.get("security", {}).get("max_actions_per_min", 10)
        while self._tracker[user_id] and now - self._tracker[user_id][0] > 60:
            self._tracker[user_id].popleft()
        self._tracker[user_id].append(now)
        return len(self._tracker[user_id]) > limit
