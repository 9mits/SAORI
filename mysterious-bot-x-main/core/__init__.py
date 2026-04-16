"""
core — shared bot infrastructure (constants, utilities, context helpers).

Import from here for clean, stable paths that won't change as the codebase
is restructured.  All symbols re-exported here come from canonical modules
in modules/ so there is no duplicate logic.
"""

from modules.mbx_constants import (
    BRAND_NAME,
    COOLDOWN_SECONDS,
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
    EMBED_PALETTE,
    FEATURE_FLAG_LABELS,
    HOLO_PRIMARY,
    HOLO_SECONDARY,
    HOLO_TERTIARY,
    MODMAIL_PANEL_BANNER_URL,
    MODMAIL_PANEL_CATEGORIES,
    SCOPE_ANALYTICS,
    SCOPE_MODERATION,
    SCOPE_ROLES,
    SCOPE_SUPPORT,
    SCOPE_SYSTEM,
    THEME_ORANGE,
    TOKEN_ENV_VARS,
)
from modules.mbx_utils import (
    create_progress_bar,
    extract_snowflake_id,
    format_duration,
    iso_to_dt,
    now_iso,
    parse_duration_str,
    truncate_text,
)
from modules.mbx_context import get_bot, set_bot
from storage import get_guild_store, reset_guild_store, set_guild_store

__all__ = [
    # constants
    "BRAND_NAME", "COOLDOWN_SECONDS", "DEFAULT_ANCHOR_ROLE_ID",
    "DEFAULT_ARCHIVE_CAT_ID", "DEFAULT_MAX_UNREAD_PINGS",
    "DEFAULT_MESSAGE_CACHE_LIMIT", "DEFAULT_MESSAGE_CACHE_RETENTION_DAYS",
    "DEFAULT_ROLE_ADMIN", "DEFAULT_ROLE_COMMUNITY_MANAGER",
    "DEFAULT_ROLE_MOD", "DEFAULT_ROLE_OWNER", "DEFAULT_RULES",
    "DEFAULT_SPAM_ROLE_ID", "EMBED_PALETTE", "FEATURE_FLAG_LABELS",
    "HOLO_PRIMARY", "HOLO_SECONDARY", "HOLO_TERTIARY",
    "MODMAIL_PANEL_BANNER_URL", "MODMAIL_PANEL_CATEGORIES",
    "SCOPE_ANALYTICS", "SCOPE_MODERATION", "SCOPE_ROLES",
    "SCOPE_SUPPORT", "SCOPE_SYSTEM", "THEME_ORANGE", "TOKEN_ENV_VARS",
    # utils
    "create_progress_bar", "extract_snowflake_id", "format_duration",
    "iso_to_dt", "now_iso", "parse_duration_str", "truncate_text",
    # context
    "get_bot", "set_bot",
    # guild context
    "get_guild_store", "reset_guild_store", "set_guild_store",
]
