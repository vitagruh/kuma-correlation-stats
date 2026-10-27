from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kuma_stats.config import (
    Config,
    ConfigError,
    example_config_dict,
    load_config,
)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, payload: dict) -> Path:
        path = self.root / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_example_config_is_valid(self):
        config = Config.from_dict(example_config_dict())
        config.validate()
        self.assertTrue(config.kuma.verify_tls)
        self.assertEqual(config.report.timezone, "Europe/Moscow")
        self.assertFalse(config.email.enabled)

    def test_env_token_overrides(self):
        payload = example_config_dict()
        path = self._write(payload)
        with mock.patch.dict(os.environ, {"KUMA_TOKEN": "from-env"}, clear=False):
            config = load_config(path)
        self.assertEqual(config.kuma.token, "from-env")

    def test_token_file(self):
        token_path = self.root / "token.txt"
        token_path.write_text("file-token\n", encoding="utf-8")
        payload = example_config_dict()
        payload["kuma"]["token"] = ""
        payload["kuma"]["token_file"] = str(token_path)
        config = Config.from_dict(payload)
        config.validate()
        self.assertEqual(config.kuma.resolved_token(), "file-token")

    def test_unknown_section(self):
        payload = example_config_dict()
        payload["unexpected"] = {}
        with self.assertRaises(ConfigError):
            Config.from_dict(payload)

    def test_unknown_parameter(self):
        payload = example_config_dict()
        payload["kuma"]["unknown"] = 1
        with self.assertRaises(ConfigError):
            Config.from_dict(payload)

    def test_email_string_to_list(self):
        payload = example_config_dict()
        payload["email"]["to"] = "a@example.local, b@example.local"
        config = Config.from_dict(payload)
        self.assertEqual(config.email.to, ["a@example.local", "b@example.local"])

    def test_invalid_timezone(self):
        payload = example_config_dict()
        payload["report"]["timezone"] = "Nowhere/Zone"
        payload["report"]["utc_offset_hours"] = None
        config = Config.from_dict(payload)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_missing_credentials(self):
        payload = example_config_dict()
        payload["kuma"]["token"] = ""
        payload["kuma"]["token_file"] = ""
        config = Config.from_dict(payload)
        with self.assertRaises(ConfigError):
            config.validate()

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_config(self.root / "nope.json")

    def test_email_requires_recipients(self):
        payload = example_config_dict()
        payload["email"]["enabled"] = True
        payload["email"]["to"] = []
        config = Config.from_dict(payload)
        with self.assertRaises(ConfigError):
            config.validate()


if __name__ == "__main__":
    unittest.main()
