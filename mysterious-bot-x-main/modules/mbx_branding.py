"""
modules/mbx_branding.py — Per-guild custom branding commands.

Each guild can set its own bot display name (nickname), icon URL, banner URL,
and embed accent color.  Changes take effect immediately for all new embeds.
"""
from __future__ import annotations

import re
from typing import Optional

import discord
from discord import app_commands

from modules.mbx_constants import BRAND_NAME, SCOPE_SYSTEM
from modules.mbx_legacy import make_embed, is_staff

_HEX_RE = re.compile(r"^#?([0-9A-Fa-f]{6})$")


def _parse_color(hex_color: str) -> Optional[int]:
    m = _HEX_RE.match(hex_color.strip())
    return int(m.group(1), 16) if m else None


def _url_looks_valid(url: str) -> bool:
    return url.startswith("https://") or url.startswith("http://")


class BrandingGroup(app_commands.Group):
    def __init__(self) -> None:
        super().__init__(
            name="branding",
            description="Custom branding for this server (admin only)",
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_staff(interaction):
            await interaction.response.send_message(
                "You need a staff role to manage branding.", ephemeral=True
            )
            return False
        return True

    # ------------------------------------------------------------------
    @app_commands.command(name="view", description="Show current branding settings")
    async def view(self, interaction: discord.Interaction) -> None:
        cfg = interaction.client.data_manager.config
        name = cfg.get("brand_name") or f"*(default: {BRAND_NAME})*"
        icon = cfg.get("brand_icon_url") or "*(default: server icon)*"
        banner = cfg.get("brand_banner_url") or "*(default: built-in banner)*"
        raw_color = cfg.get("brand_color") or 0
        color_str = f"`#{raw_color:06X}`" if raw_color else "*(default: orange)*"

        embed = make_embed(
            "Server Branding",
            scope=SCOPE_SYSTEM,
            guild=interaction.guild,
        )
        embed.add_field(name="Display Name", value=name, inline=False)
        embed.add_field(name="Icon URL", value=icon, inline=False)
        embed.add_field(name="Banner URL", value=banner, inline=False)
        embed.add_field(name="Accent Color", value=color_str, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="name",
        description="Set the bot's display name (nickname) for this server",
    )
    @app_commands.describe(name="New display name — leave blank to reset to default")
    async def set_name(
        self, interaction: discord.Interaction, name: str = ""
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        cfg = interaction.client.data_manager.config
        cfg["brand_name"] = name.strip()
        interaction.client.data_manager._store()._dirty_config = True

        # Change the bot's nickname in this guild
        try:
            nick = name.strip() or None
            await interaction.guild.me.edit(nick=nick)
        except discord.Forbidden:
            pass

        display = f"**{name.strip()}**" if name.strip() else f"reset to `{BRAND_NAME}`"
        await interaction.followup.send(f"Display name {display}.", ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="icon",
        description="Set the icon URL used in embed footers for this server",
    )
    @app_commands.describe(url="Direct image URL (https://…) — leave blank to reset")
    async def set_icon(
        self, interaction: discord.Interaction, url: str = ""
    ) -> None:
        url = url.strip()
        if url and not _url_looks_valid(url):
            await interaction.response.send_message(
                "URL must start with `https://` or `http://`.", ephemeral=True
            )
            return

        cfg = interaction.client.data_manager.config
        cfg["brand_icon_url"] = url
        interaction.client.data_manager._store()._dirty_config = True

        msg = f"Icon URL set to <{url}>." if url else "Icon reset to server default."
        await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="banner",
        description="Set the banner image shown in the modmail panel for this server",
    )
    @app_commands.describe(url="Direct image URL (https://…) — leave blank to reset")
    async def set_banner(
        self, interaction: discord.Interaction, url: str = ""
    ) -> None:
        url = url.strip()
        if url and not _url_looks_valid(url):
            await interaction.response.send_message(
                "URL must start with `https://` or `http://`.", ephemeral=True
            )
            return

        cfg = interaction.client.data_manager.config
        cfg["brand_banner_url"] = url
        interaction.client.data_manager._store()._dirty_config = True

        msg = f"Banner URL set to <{url}>." if url else "Banner reset to built-in default."
        await interaction.response.send_message(msg, ephemeral=True)

    # ------------------------------------------------------------------
    @app_commands.command(
        name="color",
        description="Set the embed accent color for this server",
    )
    @app_commands.describe(hex_color="Hex color e.g. `#FF9900` — leave blank to reset")
    async def set_color(
        self, interaction: discord.Interaction, hex_color: str = ""
    ) -> None:
        hex_color = hex_color.strip()
        if hex_color:
            value = _parse_color(hex_color)
            if value is None:
                await interaction.response.send_message(
                    "Invalid hex color. Use format `#RRGGBB` or `RRGGBB`.", ephemeral=True
                )
                return
        else:
            value = 0

        cfg = interaction.client.data_manager.config
        cfg["brand_color"] = value
        interaction.client.data_manager._store()._dirty_config = True

        if value:
            swatch = discord.Embed(color=discord.Color(value))
            await interaction.response.send_message(
                f"Accent color set to `#{value:06X}`.", embed=swatch, ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Accent color reset to default orange.", ephemeral=True
            )
