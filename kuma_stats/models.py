"""Структуры данных, которыми обмениваются модули."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryStat:
    name: str
    sql: str
    rows: int = 0
    duration_ms: int = 0
    ok: bool = True
    error: str = ""


@dataclass
class NamedCount:
    name: str
    count: int
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "count": self.count, "meta": dict(self.meta)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NamedCount":
        return cls(
            name=str(data.get("name") or ""),
            count=int(data.get("count") or 0),
            meta=dict(data.get("meta") or {}),
        )


@dataclass
class RuleCount:
    name: str
    count: int
    severity: str = ""
    rule_id: str = ""
    unique_sources: int | None = None
    first_seen: str = ""
    last_seen: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "count": self.count,
            "severity": self.severity,
            "rule_id": self.rule_id,
            "unique_sources": self.unique_sources,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleCount":
        return cls(
            name=str(data.get("name") or ""),
            count=int(data.get("count") or 0),
            severity=str(data.get("severity") or ""),
            rule_id=str(data.get("rule_id") or ""),
            unique_sources=_opt_int(data.get("unique_sources")),
            first_seen=str(data.get("first_seen") or ""),
            last_seen=str(data.get("last_seen") or ""),
        )


@dataclass
class DailyData:
    """Всё, что удалось собрать из KUMA за анализируемый период."""

    day: str
    period_from: str
    period_to: str
    generated_at: str
    timezone: str
    cluster_id: str = ""
    cluster_name: str = ""
    event_type: int = 3
    total: int = 0
    rules: list[RuleCount] = field(default_factory=list)
    rules_truncated: bool = False
    severity: list[NamedCount] = field(default_factory=list)
    priority: list[NamedCount] = field(default_factory=list)
    tactics: list[NamedCount] = field(default_factory=list)
    tenants: list[NamedCount] = field(default_factory=list)
    entities: dict[str, list[NamedCount]] = field(default_factory=dict)
    hourly: list[int] = field(default_factory=list)
    hourly_labels: list[str] = field(default_factory=list)
    hourly_rules: list[list[RuleCount]] | None = None
    hourly_partial: bool = False
    base_events: int | None = None
    aggregated_events: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    inventory: list[NamedCount] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    query_stats: list[QueryStat] = field(default_factory=list)
    duration_sec: float = 0.0
    tool_version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "period_from": self.period_from,
            "period_to": self.period_to,
            "generated_at": self.generated_at,
            "timezone": self.timezone,
            "cluster_id": self.cluster_id,
            "cluster_name": self.cluster_name,
            "event_type": self.event_type,
            "total": self.total,
            "rules": [r.to_dict() for r in self.rules],
            "rules_truncated": self.rules_truncated,
            "severity": [x.to_dict() for x in self.severity],
            "priority": [x.to_dict() for x in self.priority],
            "tactics": [x.to_dict() for x in self.tactics],
            "tenants": [x.to_dict() for x in self.tenants],
            "entities": {
                key: [x.to_dict() for x in values]
                for key, values in self.entities.items()
            },
            "hourly": list(self.hourly),
            "hourly_labels": list(self.hourly_labels),
            "hourly_rules": (
                None
                if self.hourly_rules is None
                else [[r.to_dict() for r in bucket] for bucket in self.hourly_rules]
            ),
            "hourly_partial": self.hourly_partial,
            "base_events": self.base_events,
            "aggregated_events": self.aggregated_events,
            "raw": dict(self.raw),
            "inventory": [x.to_dict() for x in self.inventory],
            "warnings": list(self.warnings),
            "query_stats": [
                {
                    "name": q.name,
                    "sql": q.sql,
                    "rows": q.rows,
                    "duration_ms": q.duration_ms,
                    "ok": q.ok,
                    "error": q.error,
                }
                for q in self.query_stats
            ],
            "duration_sec": self.duration_sec,
            "tool_version": self.tool_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DailyData":
        if not isinstance(data, dict):
            raise ValueError("Ожидался JSON-объект с данными отчёта")
        return cls(
            day=str(data.get("day") or ""),
            period_from=str(data.get("period_from") or ""),
            period_to=str(data.get("period_to") or ""),
            generated_at=str(data.get("generated_at") or ""),
            timezone=str(data.get("timezone") or "UTC"),
            cluster_id=str(data.get("cluster_id") or ""),
            cluster_name=str(data.get("cluster_name") or ""),
            event_type=int(data.get("event_type") or 3),
            total=int(data.get("total") or 0),
            rules=[RuleCount.from_dict(x) for x in data.get("rules") or []],
            rules_truncated=bool(data.get("rules_truncated")),
            severity=[NamedCount.from_dict(x) for x in data.get("severity") or []],
            priority=[NamedCount.from_dict(x) for x in data.get("priority") or []],
            tactics=[NamedCount.from_dict(x) for x in data.get("tactics") or []],
            tenants=[NamedCount.from_dict(x) for x in data.get("tenants") or []],
            entities={
                str(key): [NamedCount.from_dict(x) for x in values]
                for key, values in (data.get("entities") or {}).items()
            },
            hourly=[int(x or 0) for x in data.get("hourly") or []],
            hourly_labels=[str(x) for x in data.get("hourly_labels") or []],
            hourly_rules=(
                None
                if data.get("hourly_rules") is None
                else [
                    [RuleCount.from_dict(x) for x in bucket]
                    for bucket in data.get("hourly_rules") or []
                ]
            ),
            hourly_partial=bool(data.get("hourly_partial")),
            base_events=_opt_int(data.get("base_events")),
            aggregated_events=_opt_int(data.get("aggregated_events")),
            raw=dict(data.get("raw") or {}),
            inventory=[NamedCount.from_dict(x) for x in data.get("inventory") or []],
            warnings=[str(x) for x in data.get("warnings") or []],
            query_stats=[
                QueryStat(
                    name=str(q.get("name") or ""),
                    sql=str(q.get("sql") or ""),
                    rows=int(q.get("rows") or 0),
                    duration_ms=int(q.get("duration_ms") or 0),
                    ok=bool(q.get("ok")),
                    error=str(q.get("error") or ""),
                )
                for q in data.get("query_stats") or []
            ],
            duration_sec=float(data.get("duration_sec") or 0.0),
            tool_version=str(data.get("tool_version") or ""),
        )


@dataclass
class RuleStats:
    mean: float
    std: float
    median: float
    max_seen: int
    days_seen: int
    mean7: float
    prev_count: int
    last_seen_day: str


@dataclass
class Baseline:
    days_available: int
    window_days: int
    enough_data: bool
    days: list[str]
    totals: dict[str, int]
    prev_total: int | None
    mean: float | None
    std: float | None
    mean7: float | None
    rule_stats: dict[str, RuleStats]
    total_z: float | None
    total_delta_pct: float | None


@dataclass
class RuleRow:
    name: str
    count: int
    share_pct: float
    severity: str = ""
    rule_id: str = ""
    prev_count: int | None = None
    mean7: float | None = None
    mean: float | None = None
    std: float | None = None
    median: float | None = None
    max_seen: int | None = None
    days_seen: int = 0
    delta_pct: float | None = None
    z: float | None = None
    flags: list[str] = field(default_factory=list)
    unique_sources: int | None = None
    first_seen: str = ""
    last_seen: str = ""
    last_seen_day: str = ""


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
