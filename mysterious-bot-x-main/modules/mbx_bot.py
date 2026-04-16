"""
modules/mbx_bot.py — Bot class, startup, and background tasks.

Multi-server design
===================
* bot.guild_manager  (GuildDataManager) — owns one GuildStore per Discord server.
* bot.data_manager   (_StoreProxy)      — context-variable proxy used by legacy code.
  When a command or event fires, the cog sets the guild context via
  set_guild_store(); legacy code then reads bot.data_manager.config etc.
  transparently from the right guild's store.
* Background tasks iterate bot.guild_manager.iter_stores() so every guild's
  data is processed independently.
* discord.ui.View / discord.ui.Modal are monkey-patched on startup so that
  ALL button / select / modal interactions automatically set the guild context
  before the callback fires — no per-class changes required.
"""
from __future__ import annotations

import functools
import logging
import time
from datetime import timedelta
from typing import Dict, Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from modules.mbx_constants import SCOPE_ROLES, SCOPE_SUPPORT
from modules.mbx_context import set_bot
from modules.mbx_data import resolve_bot_token
from modules.mbx_services import get_feature_flag, ticket_needs_sla_alert
from modules.mbx_utils import iso_to_dt, now_iso
from storage import GuildDataManager, set_guild_store, reset_guild_store
from storage.guild_store import GuildStore

logger = logging.getLogger("MGXBot")

EXTENSIONS = (
    "cogs.roles",
    "cogs.moderation",
    "cogs.modmail",
    "cogs.automod",
    "cogs.system",
    "cogs.branding",
)

BASE_DIR_NAME = "database"


def _build_intents() -> discord.Intents:
    intents = discord.Intents.default()
    intents.guilds = True
    intents.members = True
    intents.message_content = True
    if hasattr(intents, "auto_moderation_configuration"):
        intents.auto_moderation_configuration = True
    if hasattr(intents, "auto_moderation_execution"):
        intents.auto_moderation_execution = True
    return intents


# ---------------------------------------------------------------------------
# Guild-context injection helpers
# ---------------------------------------------------------------------------

def _wrap_command_for_guild_context(cmd: app_commands.Command, bot: "MGXBot") -> None:
    """Wrap a slash command's callback to set guild context before execution."""
    original = cmd.callback

    @functools.wraps(original)
    async def wrapped(interaction: discord.Interaction, *args, **kwargs):
        token = None
        if interaction.guild_id:
            store = bot.guild_manager.get_store(interaction.guild_id)
            token = set_guild_store(store)
        try:
            return await original(interaction, *args, **kwargs)
        finally:
            if token is not None:
                reset_guild_store(token)

    cmd.callback = wrapped


def _wrap_group_for_guild_context(group: app_commands.Group, bot: "MGXBot") -> None:
    """Recursively wrap all commands in a command group."""
    for cmd in group.commands:
        if isinstance(cmd, app_commands.Group):
            _wrap_group_for_guild_context(cmd, bot)
        elif isinstance(cmd, app_commands.Command):
            _wrap_command_for_guild_context(cmd, bot)


def _install_guild_context_patches(bot: "MGXBot") -> None:
    """
    Monkey-patch discord.ui.View and discord.ui.Modal so that ALL component
    interactions (buttons, selects, modals) automatically set the correct
    guild store before the callback fires.

    This runs once during setup_hook, before any extensions are loaded.
    """
    # ── View: patch interaction_check ─────────────────────────────────────
    _orig_view_check = discord.ui.View.interaction_check.__func__

    async def _guild_ctx_view_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id:
            store = bot.guild_manager.get_store(interaction.guild_id)
            set_guild_store(store)
        return await _orig_view_check(self, interaction)

    discord.ui.View.interaction_check = _guild_ctx_view_check

    # ── Modal: patch _scheduled_task (called before on_submit) ────────────
    if hasattr(discord.ui.Modal, "_scheduled_task"):
        _orig_modal_task = discord.ui.Modal._scheduled_task

        async def _guild_ctx_modal_task(self, interaction: discord.Interaction):
            if interaction.guild_id:
                store = bot.guild_manager.get_store(interaction.guild_id)
                set_guild_store(store)
            await _orig_modal_task(self, interaction)

        discord.ui.Modal._scheduled_task = _guild_ctx_modal_task

    logger.debug("Guild-context patches installed on View and Modal.")


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class MGXBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session: Optional[aiohttp.ClientSession] = None
        self.guild_manager: Optional[GuildDataManager] = None
        # data_manager is the context-variable proxy; set in setup_hook
        self.data_manager = None
        self.start_time = time.time()
        self.active_executions: dict = {}
        self.dm_modmail_prompt_cooldowns: Dict[int, float] = {}
        self.native_automod_event_cache: Dict[Tuple[int, int, int, str, str], float] = {}
        self.abuse_system = None

    async def setup_hook(self) -> None:
        from pathlib import Path
        from modules.mbx_data import AntiAbuseSystem

        self.session = aiohttp.ClientSession()

        # ── guild-aware data manager ───────────────────────────────────────
        base_dir = Path(__file__).resolve().parent.parent / BASE_DIR_NAME
        self.guild_manager = GuildDataManager(self, base_dir)
        await self.guild_manager.startup()

        # Proxy for legacy code (bot.data_manager.config etc.)
        from storage.manager import _StoreProxy
        self.data_manager = _StoreProxy(self.guild_manager)

        self.abuse_system = AntiAbuseSystem()

        # ── guild-context patches for Views / Modals ───────────────────────
        _install_guild_context_patches(self)

        # ── load extensions ────────────────────────────────────────────────
        for extension in EXTENSIONS:
            await self.load_extension(extension)

        # Wrap all tree commands so interactions set guild context
        for cmd in self.tree.get_commands():
            if isinstance(cmd, app_commands.Group):
                _wrap_group_for_guild_context(cmd, self)
            elif isinstance(cmd, app_commands.Command):
                _wrap_command_for_guild_context(cmd, self)

        await self._restore_persistent_views()

        self.check_tempbans.start()
        self.background_save_task.start()
        self.status_task.start()
        self.modmail_sla_task.start()
        self.role_cleanup_task.start()

    async def _restore_persistent_views(self) -> None:
        from ui.modmail import ModmailControlView, ModmailPanelView

        self.add_view(ModmailPanelView())
        if not self.guild_manager:
            return

        for store in self.guild_manager.iter_stores():
            for uid, data in store.modmail.items():
                if data.get("status") == "open":
                    log_id = data.get("log_id")
                    if log_id:
                        self.add_view(ModmailControlView(uid), message_id=log_id)

    async def close(self) -> None:
        for loop in (
            self.check_tempbans,
            self.background_save_task,
            self.status_task,
            self.modmail_sla_task,
            self.role_cleanup_task,
        ):
            loop.cancel()

        if self.guild_manager:
            await self.guild_manager.save_all(force=True)
        if self.session:
            await self.session.close()
        await super().close()

    # ── guild lifecycle ────────────────────────────────────────────────────

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if self.guild_manager:
            await self.guild_manager.on_guild_join(guild)

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        if self.guild_manager:
            self.guild_manager.on_guild_remove(guild)

    # ── background tasks ───────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_tempbans(self) -> None:
        if not self.guild_manager:
            return
        now = discord.utils.utcnow()
        for store in self.guild_manager.iter_stores():
            guild = self.get_guild(store.guild_id)
            if guild is None:
                continue
            changed = False
            for uid, records in store.punishments.items():
                for record in records:
                    if record.get("type") != "ban" or not record.get("active", False):
                        continue
                    minutes = record.get("duration_minutes", 0)
                    if minutes > 0:
                        issued_at = iso_to_dt(record.get("timestamp"))
                        if issued_at and now >= issued_at + timedelta(minutes=minutes):
                            try:
                                await guild.unban(
                                    discord.Object(id=int(uid)),
                                    reason="Tempban Expired",
                                )
                            except Exception:
                                pass
                            record["active"] = False
                            changed = True
            if changed:
                await store.save_punishments()

    @tasks.loop(minutes=2)
    async def background_save_task(self) -> None:
        if self.guild_manager:
            await self.guild_manager.save_all()

    @tasks.loop(minutes=30)
    async def status_task(self) -> None:
        await self.change_presence(activity=discord.Game(name="DM for modmail"))

    @tasks.loop(minutes=10)
    async def modmail_sla_task(self) -> None:
        from ui.shared import make_embed

        if not self.guild_manager:
            return

        now = discord.utils.utcnow()
        for store in self.guild_manager.iter_stores():
            if not get_feature_flag(store.config, "advanced_modmail", True):
                continue
            guild = self.get_guild(store.guild_id)
            if not guild:
                continue
            sla_minutes = max(5, int(store.config.get("modmail_sla_minutes", 60)))
            changed = False
            for ticket in store.modmail.values():
                if not isinstance(ticket, dict):
                    continue
                if not ticket_needs_sla_alert(ticket, now, sla_minutes):
                    continue
                thread_id = ticket.get("thread_id")
                thread = guild.get_thread(thread_id) if thread_id else None
                if not thread and thread_id:
                    try:
                        thread = await self.fetch_channel(thread_id)
                    except Exception:
                        thread = None
                assigned = ticket.get("assigned_moderator")
                assigned_text = f"<@{assigned}>" if assigned else "Unassigned"
                embed = make_embed(
                    "Reply Reminder",
                    f"> This ticket has not received a staff reply in over "
                    f"**{sla_minutes} minute{'s' if sla_minutes != 1 else ''}**.",
                    kind="warning",
                    scope=SCOPE_SUPPORT,
                )
                embed.add_field(name="Assigned To", value=assigned_text, inline=True)
                embed.add_field(name="SLA Threshold", value=f"{sla_minutes} min", inline=True)
                if thread:
                    try:
                        await thread.send(embed=embed)
                    except Exception:
                        pass
                ticket["last_sla_alert_at"] = now_iso()
                changed = True
            if changed:
                await store.save_modmail()

    @tasks.loop(hours=6)
    async def role_cleanup_task(self) -> None:
        from modules.mbx_logging import send_log
        from modules.mbx_roles import get_custom_role_limit
        from ui.shared import format_reason_value, make_embed

        if not self.guild_manager:
            return

        for store in self.guild_manager.iter_stores():
            if not get_feature_flag(store.config, "role_cleanup", True):
                continue
            guild = self.get_guild(store.guild_id)
            if not guild:
                continue

            # Set guild context for any config reads inside helpers
            token = set_guild_store(store)
            try:
                removed_any = False
                for user_id, record in list(store.roles.items()):
                    if not isinstance(record, dict):
                        continue
                    role_id = record.get("role_id")
                    role = guild.get_role(role_id) if role_id else None
                    member = guild.get_member(int(user_id))
                    if not member:
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except Exception:
                            member = None
                    if member and get_custom_role_limit(member) > 0:
                        continue
                    if role:
                        try:
                            await role.delete(reason="Custom role eligibility cleanup")
                        except Exception:
                            pass
                    store.roles.pop(user_id, None)
                    removed_any = True
                    embed = make_embed(
                        "Custom Role Cleanup",
                        "> A custom role was removed because the owner no longer meets eligibility.",
                        kind="warning",
                        scope=SCOPE_ROLES,
                        guild=guild,
                    )
                    embed.add_field(name="Target", value=f"<@{user_id}> (`{user_id}`)", inline=True)
                    embed.add_field(
                        name="Reason",
                        value=format_reason_value("Lost booster or approved-role eligibility", limit=300),
                        inline=False,
                    )
                    await send_log(guild, embed)
                if removed_any:
                    await store.save_roles()
            finally:
                reset_guild_store(token)

    @status_task.before_loop
    async def before_status_task(self) -> None:
        await self.wait_until_ready()

    @modmail_sla_task.before_loop
    async def before_modmail_sla_task(self) -> None:
        await self.wait_until_ready()

    @role_cleanup_task.before_loop
    async def before_role_cleanup_task(self) -> None:
        await self.wait_until_ready()


# ---------------------------------------------------------------------------
# Factory / runner
# ---------------------------------------------------------------------------

def create_bot() -> MGXBot:
    bot = MGXBot(command_prefix="!", intents=_build_intents())
    set_bot(bot)
    return bot


def run() -> None:
    bot = create_bot()
    bot.run(resolve_bot_token())
