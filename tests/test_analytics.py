from __future__ import annotations

import unittest

from kuma_stats.analytics import (
    build_rows,
    inventory_analysis,
    select_anomalies,
    silent_rules,
    total_anomaly,
)
from kuma_stats.config import Config
from kuma_stats.models import RuleCount

from helpers import make_baseline, make_data


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.data = make_data()
        self.baseline = make_baseline()

    def test_rows_have_share_and_history(self):
        rows = build_rows(self.data, self.baseline, self.config)
        self.assertEqual(len(rows), 4)
        self.assertAlmostEqual(rows[0].share_pct, 600 / 1050 * 100, places=1)
        self.assertEqual(rows[0].mean, 600)
        self.assertEqual(rows[0].days_seen, 10)

    def test_new_rule_flag(self):
        self.data.rules.append(RuleCount("R5_Новое правило", 12))
        self.data.total += 12
        rows = {row.name: row for row in build_rows(self.data, self.baseline, self.config)}
        self.assertIn("новое", rows["R5_Новое правило"].flags)

    def test_growth_and_drop_flags(self):
        self.data.rules = [
            RuleCount("R1_Много событий", 1200),
            RuleCount("R2_Среднее правило", 0),
        ]
        self.data.total = 1200
        rows = {row.name: row for row in build_rows(self.data, self.baseline, self.config)}
        self.assertIn("рост", rows["R1_Много событий"].flags)
        self.assertIn("падение", rows["R2_Среднее правило"].flags)
        self.assertGreater(rows["R1_Много событий"].z or 0, 3)

    def test_noise_flag(self):
        self.data.rules = [RuleCount("R_Sтабильное", 1000)]
        self.data.total = 1000
        self.baseline.rule_stats["R_Sтабильное"] = self.baseline.rule_stats[
            "R1_Много событий"
        ].__class__(
            mean=1000, std=5, median=1000, max_seen=1010, days_seen=10,
            mean7=1000, prev_count=1000, last_seen_day="2026-10-05",
        )
        rows = build_rows(self.data, self.baseline, self.config)
        self.assertIn("шум", rows[0].flags)

    def test_anomalies_sorted_new_first(self):
        self.data.rules = [
            RuleCount("R1_Много событий", 2000),
            RuleCount("R5_Новое", 5),
        ]
        self.data.total = 2005
        rows = build_rows(self.data, self.baseline, self.config)
        anomalies = select_anomalies(rows)
        self.assertEqual(anomalies[0].name, "R5_Новое")
        self.assertEqual(anomalies[1].name, "R1_Много событий")

    def test_silent_rules(self):
        silent = silent_rules(self.data, self.baseline, self.config)
        self.assertEqual([row.name for row in silent], ["R_Молчащее"])
        self.assertIn("молчит", silent[0].flags)
        self.assertEqual(silent[0].count, 0)

    def test_silent_skipped_when_list_truncated(self):
        self.data.rules_truncated = True
        self.assertEqual(silent_rules(self.data, self.baseline, self.config), [])

    def test_total_anomaly(self):
        self.baseline.total_z = 5.0
        self.baseline.total_delta_pct = 40.0
        state = total_anomaly(self.baseline, self.config)
        self.assertTrue(state["is_anomaly"])
        self.assertEqual(state["direction"], "рост")

    def test_total_anomaly_requires_both_thresholds(self):
        self.baseline.total_z = 5.0
        self.baseline.total_delta_pct = 5.0
        self.assertFalse(total_anomaly(self.baseline, self.config)["is_anomaly"])

    def test_inventory_analysis(self):
        info = inventory_analysis(self.data, self.baseline, self.config)
        self.assertIsNotNone(info)
        self.assertEqual([item.name for item in info["not_today"]], ["Старое правило"])
        self.assertIn("Старое правило", [item.name for item in info["long_silent"]])

    def test_no_history_marks_nothing_as_new(self):
        baseline = make_baseline(days=0)
        baseline.enough_data = False
        baseline.rule_stats = {}
        rows = build_rows(self.data, baseline, self.config)
        self.assertTrue(all(not row.flags for row in rows))
        self.assertIsNone(rows[0].mean)


if __name__ == "__main__":
    unittest.main()
