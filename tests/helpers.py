"""Общие тестовые данные."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta

from kuma_stats.models import (
    Baseline,
    DailyData,
    NamedCount,
    QueryStat,
    RuleCount,
    RuleStats,
)
from kuma_stats.utils import day_bounds, load_timezone, to_rfc3339


def make_data(day: str = "2026-10-06") -> DailyData:
    rules = [
        RuleCount("R1_Много событий", 600, "High", "rule-1", 12),
        RuleCount("R2_Среднее правило", 300, "Medium", "rule-2", 8),
        RuleCount("R3_Редкое правило", 100, "Low", "rule-3", 3),
        RuleCount("R4_Критичное", 50, "Critical", "rule-4", 2),
    ]
    total = sum(rule.count for rule in rules)
    start, _ = day_bounds(date.fromisoformat(day), load_timezone("UTC", 0)[0])
    hourly = [total // 24 + (1 if index < total % 24 else 0) for index in range(24)]
    labels = [f"{hour:02d}:00" for hour in range(24)]
    hourly_rules = [
        [RuleCount("R1_Много событий", hourly[index])] for index in range(24)
    ]
    return DailyData(
        day=day,
        period_from=to_rfc3339(start),
        period_to=to_rfc3339(start + timedelta(days=1)),
        generated_at=to_rfc3339(datetime(2026, 10, 7, 1, 0, tzinfo=start.tzinfo)),
        timezone="Europe/Moscow",
        cluster_id="cluster-1",
        cluster_name="Test cluster",
        total=total,
        rules=rules,
        severity=[
            NamedCount("high", 600),
            NamedCount("medium", 300),
            NamedCount("low", 100),
            NamedCount("critical", 50),
        ],
        priority=[NamedCount("1", 500), NamedCount("2", 550)],
        tactics=[
            NamedCount("Credential Access", 400),
            NamedCount("Discovery", 350),
            NamedCount("Execution", 300),
        ],
        tenants=[
            NamedCount("tenant-1", 700, {"name": "HQ"}),
            NamedCount("tenant-2", 350, {"name": "Branch"}),
        ],
        entities={
            "SourceAddress": [
                NamedCount("10.0.0.1", 300),
                NamedCount("10.0.0.2", 200),
            ],
            "SourceUserName": [NamedCount("user1", 400)],
            "DestinationAddress": [NamedCount("192.168.1.10", 250)],
            "DeviceHostName": [NamedCount("host-01", 450)],
        },
        hourly=hourly,
        hourly_labels=labels,
        hourly_rules=hourly_rules,
        base_events=50000,
        aggregated_events=1000,
        raw={
            "enabled": True,
            "events": total,
            "truncated": False,
            "invalid_timestamps": 0,
            "unique": {"SourceAddress": 24, "SourceUserName": 8},
            "rules": {
                rule.name: {
                    "events": rule.count,
                    "severity": rule.severity,
                    "rule_id": rule.rule_id,
                    "sources": rule.unique_sources or 0,
                    "first": "",
                    "last": "",
                }
                for rule in rules
            },
            "severity": [{"name": "high", "count": 600}],
            "tactics": [{"name": "Credential Access", "count": 400}],
            "tenants": [{"name": "tenant-1", "count": 700}],
            "entities": {},
            "pairs": [],
            "rule_hosts": [],
        },
        inventory=[
            NamedCount("R1_Много событий", 0, {"id": "res-1", "tenant": "HQ"}),
            NamedCount("R2_Среднее правило", 0, {"id": "res-2", "tenant": "HQ"}),
            NamedCount("Старое правило", 0, {"id": "res-3", "tenant": "Branch"}),
        ],
        warnings=[],
        query_stats=[
            QueryStat("total", "SELECT count(ID) FROM `events` WHERE Type = 3", 1, 20),
            QueryStat("rules", "SELECT count(ID), Name ...", 4, 35),
            QueryStat(
                "hourly_00",
                "SELECT count(ID), Name ... LIMIT 250",
                1,
                15,
            ),
        ],
        duration_sec=12.5,
        tool_version="1.0.0",
    )


def make_baseline(days: int = 10) -> Baseline:
    if days <= 0:
        return Baseline(
            days_available=0,
            window_days=30,
            enough_data=False,
            days=[],
            totals={},
            prev_total=None,
            mean=None,
            std=None,
            mean7=None,
            rule_stats={},
            total_z=None,
            total_delta_pct=None,
        )
    day_list = [
        (date(2026, 10, 6) - timedelta(days=offset)).isoformat()
        for offset in range(days, 0, -1)
    ]
    totals = {day: 1000 for day in day_list}
    rule_stats = {
        "R1_Много событий": RuleStats(
            mean=600, std=60, median=600, max_seen=720, days_seen=10,
            mean7=610, prev_count=590, last_seen_day=day_list[-1],
        ),
        "R2_Среднее правило": RuleStats(
            mean=300, std=30, median=300, max_seen=350, days_seen=10,
            mean7=305, prev_count=298, last_seen_day=day_list[-1],
        ),
        "R3_Редкое правило": RuleStats(
            mean=100, std=10, median=100, max_seen=120, days_seen=10,
            mean7=101, prev_count=99, last_seen_day=day_list[-1],
        ),
        "R_Молчащее": RuleStats(
            mean=40, std=5, median=40, max_seen=50, days_seen=10,
            mean7=40, prev_count=41, last_seen_day=day_list[-1],
        ),
    }
    return Baseline(
        days_available=days,
        window_days=30,
        enough_data=days >= 7,
        days=day_list,
        totals=totals,
        prev_total=1000,
        mean=1000.0,
        std=40.0,
        mean7=1000.0,
        rule_stats=rule_stats,
        total_z=0.0,
        total_delta_pct=0.0,
    )


class FakeKumaClient:
    """Минимальный двойник KumaClient для проверки Collector."""

    def __init__(
        self,
        events: list[dict],
        *,
        fail_rules_with_severity: bool = False,
        fail_hourly: bool = False,
        inventory: list[dict] | None = None,
    ):
        self.events = events
        self.fail_rules_with_severity = fail_rules_with_severity
        self.fail_hourly = fail_hourly
        self.inventory = inventory or []
        self.queries: list[str] = []

    def whoami(self):
        return {"login": "tester"}

    def resolve_cluster(self):
        return "cluster-1", "Test cluster"

    def get_tenants(self):
        return [{"id": "tenant-1", "name": "HQ"}]

    def get_correlation_rules(self):
        return self.inventory

    def search_events(self, sql, period_from, period_to, cluster_id=None):
        from kuma_stats.kuma_client import KumaError

        self.queries.append(sql)
        if self.fail_rules_with_severity and "GROUP BY Name, Severity" in sql:
            self.fail_rules_with_severity = False
            raise KumaError("severity is not groupable")

        start = datetime.fromisoformat(period_from.replace("Z", "+00:00"))
        end = datetime.fromisoformat(period_to.replace("Z", "+00:00"))
        if (
            self.fail_hourly
            and "GROUP BY Name" in sql
            and (end - start) <= timedelta(hours=2)
        ):
            raise KumaError("hourly query failed")
        inside = [
            event
            for event in self.events
            if start
            <= datetime.fromisoformat(event["Timestamp"].replace("Z", "+00:00"))
            <= end
        ]
        if "SELECT Timestamp" in sql:
            return inside
        if "GROUP BY" not in sql:
            if "Type = 1" in sql:
                return [{"count(ID)": 1000}]
            if "Type = 2" in sql:
                return [{"count(ID)": 10}]
            return [{"count(ID)": len(inside)}]
        if "GROUP BY Name, Severity" in sql:
            return _group(inside, ["Name", "Severity"])
        if "GROUP BY Name" in sql:
            return _group(inside, ["Name"])
        if "GROUP BY Severity" in sql:
            return _group(inside, ["Severity"])
        if "GROUP BY Tactic" in sql:
            return _group(inside, ["Tactic"])
        if "GROUP BY TenantID" in sql:
            return _group(inside, ["TenantID"])
        for field in ("SourceAddress", "SourceUserName", "DestinationAddress", "DeviceHostName"):
            if f"GROUP BY {field}" in sql:
                return _group(inside, [field])
        return []


def _group(rows: list[dict], fields: list[str]) -> list[dict]:
    buckets: dict[tuple, int] = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in fields)
        buckets[key] = buckets.get(key, 0) + 1
    result = []
    for key, count in sorted(buckets.items(), key=lambda item: -item[1]):
        item = dict(zip(fields, key))
        item["count(ID)"] = count
        result.append(item)
    return result


def make_events(day: date = date(2026, 10, 6), count: int = 120) -> list[dict]:
    rng = random.Random(7)
    tz, _ = load_timezone("Europe/Moscow", 3.0)
    start, end = day_bounds(day, tz)
    span = int((end - start).total_seconds())
    names = ["R1_Много событий", "R2_Среднее правило", "R3_Редкое правило"]
    events = []
    for index in range(count):
        name = names[index % len(names)]
        moment = start + timedelta(seconds=rng.randrange(0, span))
        events.append(
            {
                "Timestamp": to_rfc3339(moment),
                "Name": name,
                "CorrelationRuleID": f"rule-{index % len(names)}",
                "Severity": ["High", "Medium", "Low"][index % 3],
                "Priority": "1",
                "SourceAddress": f"10.0.0.{index % 10 + 1}",
                "SourceUserName": f"user{index % 4}",
                "DestinationAddress": "192.168.1.10",
                "DestinationUserName": "srv1",
                "DeviceHostName": f"host-{index % 3}",
                "SourceHostName": f"host-{index % 3}",
                "Tactic": "Discovery",
                "Technique": "T1018",
                "BaseEventCount": "1",
                "TenantID": "tenant-1",
            }
        )
    events.sort(key=lambda item: item["Timestamp"])
    return events
