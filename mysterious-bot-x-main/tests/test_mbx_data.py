import asyncio
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

import discord

from modules import mbx_data
from storage.guild_store import GuildStore


class MbxDataTests(unittest.TestCase):
    def setUp(self):
        # GuildStore now requires (guild_id, base_dir); use a temp dir
        self._tmp = tempfile.TemporaryDirectory()
        self.manager = GuildStore(0, Path(self._tmp.name))
        self.manager.config = {"case_counter": 0}

    def tearDown(self):
        self._tmp.cleanup()

    def test_allocate_case_id_increments_counter(self):
        self.assertEqual(self.manager.allocate_case_id(), 1)
        self.assertEqual(self.manager.config["case_counter"], 1)

    def test_prepare_punishment_record_adds_case_id_and_timestamp(self):
        record = self.manager.prepare_punishment_record({"type": "warn", "reason": "Test"})
        self.assertIn("case_id", record)
        self.assertIn("timestamp", record)
        self.assertFalse(record["active"])

    def test_message_cache_normalization_coerces_ids(self):
        normalized = self.manager._normalize_message_cache_record(
            {"id": "42", "author_id": "7", "channel_id": "9", "created_at": "2026-01-01T00:00:00+00:00"}
        )
        self.assertEqual(normalized["id"], 42)
        self.assertEqual(normalized["author_id"], 7)
        self.assertEqual(normalized["channel_id"], 9)
        self.assertIsInstance(normalized["created_at"], type(discord.utils.utcnow()))

    def test_load_all_initializes_defaults_and_migrations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            guild_id = 123456789
            # New per-guild layout
            guild_dir = base / "guilds" / str(guild_id)
            guild_dir.mkdir(parents=True)
            for name, payload in {
                "config.json": "{}",
                "roles.json": "{}",
                "punishments.json": "{}",
                "mod_stats.json": "{}",
                "message_cache.json": "[]",
                "pings.json": "{}",
                "modmail.json": "{}",
                "lockdown.json": "{}",
            }.items():
                (guild_dir / name).write_text(payload, encoding="utf-8")

            store = GuildStore(guild_id, base)
            asyncio.run(store.load_all())
            self.assertIn("feature_flags", store.config)
            self.assertIsInstance(store.message_cache, deque)
            self.assertEqual(store.config.get("guild_id"), guild_id)

    def test_resolve_bot_token_prefers_environment_variable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.json"
            config_file.write_text('{"token_env_var": "CUSTOM_BOT_TOKEN", "bot_token": "config-secret"}', encoding="utf-8")

            with patch.object(mbx_data, "CONFIG_FILE", config_file), patch.dict(os.environ, {"CUSTOM_BOT_TOKEN": "env-secret"}, clear=True):
                self.assertEqual(mbx_data.resolve_bot_token(), "env-secret")

    def test_resolve_bot_token_rejects_config_json_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / "config.json"
            config_file.write_text('{"bot_token": "config-secret"}', encoding="utf-8")

            with patch.object(mbx_data, "CONFIG_FILE", config_file), patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    mbx_data.resolve_bot_token()


if __name__ == "__main__":
    unittest.main()
