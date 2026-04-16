"""
modules/mbx_data.py — backwards-compatibility shim.

The DataManager class that used to live here has been replaced by:

  storage.GuildStore          — per-guild data store
  storage.GuildDataManager    — multi-guild manager owned by the bot

This module re-exports the symbols that other parts of the codebase still
import from here, and keeps resolve_bot_token() and AntiAbuseSystem in place.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import discord

from modules.mbx_constants import TOKEN_ENV_VARS

# Re-export the new storage classes under their legacy names so existing
# imports (from modules.mbx_data import DataManager) still work.
from storage import GuildStore as DataManager           # noqa: F401
from storage import GuildDataManager                    # noqa: F401

logger = logging.getLogger("MGXBot")

# Kept here so mbx_bot and tests can still import it
BASE_DIR = Path(__file__).resolve().parent.parent
DB_DIR = BASE_DIR / "database"

# Legacy file-path constants (still used by resolve_bot_token for bootstrap)
CONFIG_FILE = DB_DIR / "config.json"


# ---------------------------------------------------------------------------
# Token resolution (unchanged — reads config before the bot starts)
# ---------------------------------------------------------------------------

def read_json_file(path: Path, default: Any) -> Any:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path.name, exc)
    return default


def resolve_bot_token() -> str:
    """
    Resolve the Discord bot token from environment variables.

    Checks config.json (if it still exists at the legacy path or inside
    database/guilds/<id>/config.json) for a custom env-var name, then falls
    back to the standard TOKEN_ENV_VARS.
    """
    # Try legacy flat path first, then any guild config inside guilds/
    bootstrap_config: dict = {}
    if CONFIG_FILE.exists():
        bootstrap_config = read_json_file(CONFIG_FILE, {})
    else:
        guilds_dir = DB_DIR / "guilds"
        if guilds_dir.is_dir():
            for guild_dir in guilds_dir.iterdir():
                cfg_path = guild_dir / "config.json"
                if cfg_path.exists():
                    bootstrap_config = read_json_file(cfg_path, {})
                    break

    env_var_order: List[str] = []
    configured = bootstrap_config.get("token_env_var")
    if isinstance(configured, str) and configured.strip():
        env_var_order.append(configured.strip())
    for var in TOKEN_ENV_VARS:
        if var not in env_var_order:
            env_var_order.append(var)

    for var in env_var_order:
        token = os.getenv(var)
        if token:
            return token.strip()

    raise RuntimeError(
        "Discord bot token is not configured. Set one of the supported "
        f"environment variables ({', '.join(env_var_order)})."
    )


# ---------------------------------------------------------------------------
# AntiAbuseSystem (unchanged — stateless, no guild context needed)
# ---------------------------------------------------------------------------

class AntiAbuseSystem:
    """Rate-limit and abuse tracking.  One instance lives on the bot."""

    def __init__(self) -> None:
        self._tracker: Dict[int, deque] = defaultdict(lambda: deque(maxlen=15))
        self.cooldowns: Dict[str, float] = {}
        self.mention_spam_tracker: Dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
        self.smart_automod_tracker: Dict[int, deque] = defaultdict(lambda: deque(maxlen=8))

    def check_rate_limit(self, user_id: int, config: Optional[dict] = None) -> bool:
        """Return True if the user has exceeded the action rate limit."""
        if config is None:
            config = {}
        now = time.time()
        limit = config.get("security", {}).get("max_actions_per_min", 10)
        q = self._tracker[user_id]
        while q and now - q[0] > 60:
            q.popleft()
        q.append(now)
        return len(q) > limit
