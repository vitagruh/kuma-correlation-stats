from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from kuma_stats.analytics import (
    build_rows,
    inventory_analysis,
    select_anomalies,
    silent_rules,
)
from kuma_stats.config import Config
from kuma_stats.models import DailyData
from kuma_stats.report import build_report
from kuma_stats.utils import read_json

from helpers import make_baseline, make_data


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config()
        self.config.report.output_dir = str(self.root / "reports")
        self.config.report.state_dir = str(self.root / "state")
        self.config.report.title = "KUMA тест"

    def tearDown(self):
        self.tmp.cleanup()

    def _build(self, data=None, baseline=None, **kwargs):
        data = data or make_data()
        baseline = baseline or make_baseline()
        rows = build_rows(data, baseline, self.config)
        anomalies = select_anomalies(rows)
        silent = silent_rules(data, baseline, self.config)
        inventory = inventory_analysis(data, baseline, self.config)
        return build_report(
            self.config, data, baseline, rows, anomalies, silent, inventory, **kwargs
        )

    def test_report_creates_all_files(self):
        result = self._build()
        self.assertTrue(result.html_path.is_file())
        expected = {
            "snapshot.json", "summary.json", "rules.csv", "anomalies.csv",
            "silent_rules.csv", "hourly.csv", "hourly_rules.csv",
            "entity_SourceAddress.csv", "tactics.csv", "priority.csv",
            "tenants.csv", "inventory_silent.csv", "queries.csv",
        }
        for name in expected:
            self.assertTrue((result.day_dir / name).is_file(), name)
        self.assertTrue((Path(self.config.report.output_dir) / "index.html").is_file())

    def test_html_contains_sections(self):
        result = self._build()
        html = result.html_path.read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", html)
        self.assertIn("KUMA тест", html)
        self.assertIn("Аномалии и новые правила", html)
        self.assertIn("Динамика по часам", html)
        self.assertIn("MITRE ATT&amp;CK", html)
        self.assertIn("Инвентарь правил корреляции", html)
        self.assertIn("<svg", html)
        self.assertGreater(html.count("<svg"), 5)
        self.assertNotIn("Traceback", html)

    def test_snapshot_roundtrip(self):
        result = self._build()
        payload = read_json(result.snapshot_path)
        restored = DailyData.from_dict(payload)
        self.assertEqual(restored.day, "2026-10-06")
        self.assertEqual(restored.total, 1050)
        self.assertEqual(len(restored.rules), 4)
        self.assertEqual(restored.hourly_labels[0], "00:00")
        self.assertEqual(restored.hourly_rules[0][0].name, "R1_Много событий")

    def test_summary_contains_top_rules(self):
        result = self._build()
        summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["total"], 1050)
        self.assertEqual(summary["top_rules"][0]["name"], "R1_Много событий")
        self.assertEqual(summary["tool_version"], "1.0.0")

    def test_report_without_history_and_hourly(self):
        data = make_data("2026-10-07")
        data.hourly = []
        data.hourly_labels = []
        data.hourly_rules = None
        data.rules_truncated = True
        baseline = make_baseline(days=0)
        baseline.enough_data = False
        baseline.rule_stats = {}
        baseline.days = []
        baseline.totals = {}
        baseline.mean = None
        baseline.mean7 = None
        baseline.std = None
        result = self._build(data=data, baseline=baseline)
        html = result.html_path.read_text(encoding="utf-8")
        self.assertIn("недостаточно истории", html)
        self.assertIn("Почасовая детализация отключена", html)
        self.assertIn("История пуста", html)
        self.assertIn("список сработок усечён", html)

    def test_empty_entities_and_warnings(self):
        data = make_data()
        data.entities = {"SourceAddress": []}
        data.warnings = ["Проверочное <предупреждение> & тест"]
        result = self._build(data=data)
        html = result.html_path.read_text(encoding="utf-8")
        self.assertIn("Проверочное &lt;предупреждение&gt; &amp; тест", html)
        self.assertIn("Нет данных по этому полю", html)

    def test_custom_html_path(self):
        out = self.root / "custom" / "daily.html"
        result = self._build(out_html=out, write_snapshot=False)
        self.assertEqual(result.html_path, out)
        self.assertTrue(out.is_file())
        self.assertIsNone(result.snapshot_path)

    def test_csv_is_excel_friendly(self):
        result = self._build()
        raw = (result.day_dir / "rules.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        text = raw.decode("utf-8-sig")
        self.assertIn("Правило;", text.replace("\ufeff", ""))
        self.assertIn(";", text)


if __name__ == "__main__":
    unittest.main()
