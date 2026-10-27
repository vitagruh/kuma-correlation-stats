from __future__ import annotations

import unittest
from datetime import date

from kuma_stats.collector import NO_NAME, Collector
from kuma_stats.config import Config

from helpers import FakeKumaClient, make_events


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.config.kuma.base_url = "https://kuma.test"
        self.config.kuma.token = "token"
        self.config.kuma.verify_token = False
        self.config.kuma.delay_between_queries = 0
        self.config.report.timezone = "Europe/Moscow"
        self.config.report.utc_offset_hours = 3.0
        self.day = date(2026, 10, 6)
        self.events = make_events(self.day, 120)

    def _collect(self, client, **config_updates):
        for key, value in config_updates.items():
            setattr(self.config.kuma, key, value)
        return Collector(self.config, client).collect(self.day)

    def test_main_flow(self):
        client = FakeKumaClient(
            self.events,
            inventory=[
                {"id": "res-1", "name": "R1_Много событий", "tenantName": "HQ"},
                {"id": "res-2", "name": "Старое правило", "tenantName": "HQ"},
            ],
        )
        self.config.kuma.collect_rule_inventory = True
        data = self._collect(client)

        self.assertEqual(data.day, "2026-10-06")
        self.assertEqual(data.total, 120)
        self.assertEqual(len(data.rules), 3)
        self.assertEqual({rule.name for rule in data.rules}, {
            "R1_Много событий", "R2_Среднее правило", "R3_Редкое правило",
        })
        self.assertTrue(all(rule.count == 40 for rule in data.rules))
        self.assertEqual(data.cluster_id, "cluster-1")
        self.assertEqual(data.severity[0].name, "High")
        self.assertEqual(data.base_events, 1000)
        self.assertEqual(len(data.hourly), 24)
        self.assertEqual(sum(data.hourly), 120)
        self.assertEqual(data.hourly_labels[0], "00:00")
        self.assertFalse(data.hourly_partial)
        self.assertEqual(len(data.raw["rules"]), 3)
        self.assertEqual(data.raw["unique"]["SourceAddress"], 10)
        self.assertEqual(len(data.inventory), 2)
        self.assertFalse(
            any("не выполнена" in warning for warning in data.warnings)
        )
        self.assertGreater(len(data.query_stats), 30)

    def test_rule_query_fallback_without_severity(self):
        client = FakeKumaClient(self.events, fail_rules_with_severity=True)
        data = self._collect(client)
        self.assertEqual(len(data.rules), 3)
        self.assertEqual(sum(rule.count for rule in data.rules), 120)
        self.assertTrue(all(rule.severity for rule in data.rules))
        self.assertFalse(
            any("Часть запросов не выполнена" in warning for warning in data.warnings)
        )
        names = {stat.name for stat in data.query_stats}
        self.assertIn("rules_simple", names)

    def test_rules_rebuilt_from_raw_when_truncated(self):
        client = FakeKumaClient(self.events)
        data = self._collect(client, max_rule_rows=2)
        self.assertFalse(data.rules_truncated)
        self.assertEqual(len(data.rules), 3)
        self.assertEqual(sum(rule.count for rule in data.rules), 120)
        self.assertFalse(
            any("Список правил ограничен" in warning for warning in data.warnings)
        )

    def test_hourly_rebuilt_from_raw(self):
        client = FakeKumaClient(self.events, fail_hourly=True)
        data = self._collect(client)
        self.assertEqual(sum(data.hourly), 120)
        self.assertFalse(data.hourly_partial)
        self.assertTrue(
            any("восстановлена по сырым событиям" in warning for warning in data.warnings)
        )
        self.assertFalse(
            any("Часть запросов не выполнена" in warning for warning in data.warnings)
        )

    def test_empty_day_warns(self):
        client = FakeKumaClient([])
        data = self._collect(client)
        self.assertEqual(data.total, 0)
        self.assertEqual(data.rules, [])
        self.assertTrue(
            any("не найдено ни одной сработки" in warning for warning in data.warnings)
        )
        self.assertEqual(sum(data.hourly), 0)

    def test_events_without_name_marked(self):
        events = make_events(self.day, 6)
        for event in events[:2]:
            event["Name"] = ""
        data = self._collect(FakeKumaClient(events))
        names = {rule.name for rule in data.rules}
        self.assertIn(NO_NAME, names)
        self.assertTrue(any("без названия правила" in warning for warning in data.warnings))


if __name__ == "__main__":
    unittest.main()
