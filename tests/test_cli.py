from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from kuma_stats import cli
from kuma_stats.config import Config, ConfigError
from kuma_stats.utils import write_json

from helpers import make_data


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / "config.json"
        payload = {
            "kuma": {
                "base_url": "https://kuma.test:7223",
                "token": "token",
                "verify_token": False,
            },
            "report": {
                "output_dir": str(self.root / "reports"),
                "state_dir": str(self.root / "state"),
                "timezone": "Europe/Moscow",
                "utc_offset_hours": 3,
            },
            "logging": {"level": "ERROR"},
        }
        write_json(self.config_path, payload)

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_config(self):
        target = self.root / "new.json"
        code = cli.main(["-c", str(target), "init-config", str(target)])
        self.assertEqual(code, 0)
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertIn("kuma", data)
        self.assertIn("email", data)
        self.assertIn("webhook", data)

        code = cli.main(["-c", str(target), "init-config", str(target)])
        self.assertEqual(code, 2)

    def test_render_from_snapshot(self):
        day_dir = self.root / "reports" / "2026-10-06"
        day_dir.mkdir(parents=True)
        write_json(day_dir / "snapshot.json", make_data().to_dict())

        code = cli.main(["-c", str(self.config_path), "render", str(day_dir)])
        self.assertEqual(code, 0)
        self.assertTrue((day_dir / "report.html").is_file())
        self.assertTrue((self.root / "reports" / "index.html").is_file())

    def test_render_latest_snapshot(self):
        for day in ("2026-10-05", "2026-10-06"):
            day_dir = self.root / "reports" / day
            day_dir.mkdir(parents=True)
            write_json(day_dir / "snapshot.json", make_data(day).to_dict())
        code = cli.main(["-c", str(self.config_path), "render"])
        self.assertEqual(code, 0)
        self.assertTrue(
            (self.root / "reports" / "2026-10-06" / "report.html").is_file()
        )

    def test_render_missing_snapshot(self):
        code = cli.main(["-c", str(self.config_path), "render", str(self.root / "none")])
        self.assertEqual(code, 4)

    def test_missing_config(self):
        code = cli.main(["-c", str(self.root / "absent.json"), "run"])
        self.assertEqual(code, 2)

    def test_no_command_prints_help(self):
        self.assertEqual(cli.main(["-c", str(self.config_path)]), 2)

    def test_parse_time(self):
        self.assertEqual(cli._parse_time("01:30"), (1, 30))
        with self.assertRaises(ConfigError):
            cli._parse_time("25:00")
        with self.assertRaises(ConfigError):
            cli._parse_time("bad")

    def test_parse_target_date(self):
        config = Config()
        config.report.timezone = "Europe/Moscow"
        config.report.utc_offset_hours = 3
        self.assertEqual(cli._parse_target_date("2026-10-06", config), date(2026, 10, 6))
        self.assertIsInstance(cli._parse_target_date("yesterday", config), date)
        with self.assertRaises(ConfigError):
            cli._parse_target_date("06.10.2026", config)


if __name__ == "__main__":
    unittest.main()
