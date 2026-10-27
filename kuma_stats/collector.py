"""Сбор данных из KUMA и приведение их к единому снимку дня."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from . import __version__
from .config import Config
from .kuma_client import (
    KumaAuthError,
    KumaClient,
    KumaConnectionError,
    KumaError,
    KumaHTTPError,
)
from .models import DailyData, NamedCount, QueryStat, RuleCount
from .utils import (
    day_bounds,
    hour_label,
    hour_slices,
    inclusive_period,
    load_timezone,
    now_utc,
    parse_dt,
    to_rfc3339,
)

NO_NAME = "Без названия"


class Collector:
    def __init__(self, config: Config, client: KumaClient, logger: logging.Logger | None = None):
        self.config = config
        self.client = client
        self.log = logger or logging.getLogger("kuma_stats")
        self.tz, self.tz_warning = load_timezone(
            config.report.timezone, config.report.utc_offset_hours
        )
        self.cluster_id = ""
        self.cluster_name = ""
        self.stats: list[QueryStat] = []
        self.failed: set[str] = set()
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ #
    # Публичный вход
    # ------------------------------------------------------------------ #

    def collect(self, target_day: date | None = None) -> DailyData:
        started = time.monotonic()
        start_utc, end_utc, day_label, period_note = self._resolve_period(target_day)
        if period_note:
            self.warnings.append(period_note)
        if self.tz_warning:
            self.warnings.append(self.tz_warning)

        self.cluster_id, self.cluster_name = self.client.resolve_cluster()
        self._check_token()

        self.log.info(
            "Сбор сработок за %s (%s — %s), кластер: %s",
            day_label,
            to_rfc3339(start_utc),
            to_rfc3339(end_utc),
            self.cluster_name or self.cluster_id or "по умолчанию",
        )

        event_type = int(self.config.kuma.event_type)
        total = self._collect_total(event_type, start_utc, end_utc)

        rules, rules_truncated = self._collect_rules(event_type, start_utc, end_utc)
        severity = self._collect_named("severity", event_type, "Severity", start_utc, end_utc)
        priority = (
            self._collect_named("priority", event_type, "Priority", start_utc, end_utc)
            if self.config.kuma.collect_priority
            else []
        )
        tactics = (
            self._collect_named("tactics", event_type, "Tactic", start_utc, end_utc)
            if self.config.kuma.collect_tactics
            else []
        )
        tenants = self._collect_tenants(event_type, start_utc, end_utc)
        entities = self._collect_entities(event_type, start_utc, end_utc)
        base_events, aggregated_events = self._collect_health(start_utc, end_utc)

        raw_summary, raw_events = self._collect_raw(event_type, start_utc, end_utc)

        # Резервные пути: если агрегаты неполные или недоступны, используем выборку.
        rules, rules_truncated = self._merge_rule_details(
            rules, rules_truncated, raw_summary
        )
        if not severity and raw_summary.get("severity"):
            severity = [NamedCount.from_dict(x) for x in raw_summary["severity"]]
            self.warnings.append(
                "Распределение по критичности построено по выборке сырых событий."
            )
        if not tactics and raw_summary.get("tactics"):
            tactics = [NamedCount.from_dict(x) for x in raw_summary["tactics"]]
        if not tenants and raw_summary.get("tenants"):
            tenants = [NamedCount.from_dict(x) for x in raw_summary["tenants"]]
        entities = self._merge_entities(entities, raw_summary)

        hourly, hourly_labels, hourly_rules, hourly_partial = self._collect_hourly(
            event_type, start_utc, end_utc, raw_events, raw_summary
        )
        if hourly and not hourly_partial and total:
            hourly_sum = sum(hourly)
            if abs(hourly_sum - total) > max(1, int(total * 0.005)):
                self.warnings.append(
                    f"Сумма сработок по часам ({hourly_sum}) не совпадает с общим "
                    f"числом ({total}). Проверьте границы периода и лимиты выборки."
                )

        inventory = self._collect_inventory()

        self._collect_warnings(
            total=total,
            rules=rules,
            rules_truncated=rules_truncated,
            base_events=base_events,
            raw_summary=raw_summary,
        )

        duration = time.monotonic() - started
        data = DailyData(
            day=day_label,
            period_from=to_rfc3339(start_utc),
            period_to=to_rfc3339(end_utc),
            generated_at=to_rfc3339(now_utc()),
            timezone=self.config.report.timezone,
            cluster_id=self.cluster_id,
            cluster_name=self.cluster_name,
            event_type=event_type,
            total=total,
            rules=rules,
            rules_truncated=rules_truncated,
            severity=severity,
            priority=priority,
            tactics=tactics,
            tenants=tenants,
            entities=entities,
            hourly=hourly,
            hourly_labels=hourly_labels,
            hourly_rules=hourly_rules,
            hourly_partial=hourly_partial,
            base_events=base_events,
            aggregated_events=aggregated_events,
            raw=raw_summary,
            inventory=inventory,
            warnings=_dedupe(self.warnings),
            query_stats=self.stats,
            duration_sec=round(duration, 2),
            tool_version=__version__,
        )
        self.log.info(
            "Сбор завершён: %s сработок, %s правил, %s запросов, %.1f с",
            total,
            len(rules),
            len(self.stats),
            duration,
        )
        return data

    # ------------------------------------------------------------------ #
    # Период и проверка доступа
    # ------------------------------------------------------------------ #

    def _resolve_period(
        self, target_day: date | None
    ) -> tuple[datetime, datetime, str, str]:
        now = now_utc()
        report = self.config.report
        if target_day is not None:
            if target_day > now.astimezone(self.tz).date():
                raise ValueError("Нельзя анализировать будущую дату")
            day = target_day
            label = day.isoformat()
        elif report.window == "previous_day":
            day = now.astimezone(self.tz).date() - timedelta(days=report.day_offset)
            label = day.isoformat()
        else:
            end = now
            start = end - timedelta(hours=24)
            return (
                start,
                end,
                end.astimezone(self.tz).date().isoformat(),
                "Режим last_24h: период заканчивается текущим моментом.",
            )

        start, end = day_bounds(day, self.tz)
        note = ""
        if end > now:
            note = (
                "Период ещё не завершён: данные за оставшуюся часть суток "
                "в отчёт не попадут."
            )
        return start, end, label, note

    def _check_token(self) -> None:
        if not self.config.kuma.verify_token:
            return
        try:
            info = self.client.whoami()
        except KumaHTTPError as exc:
            if exc.status == 404:
                self.log.warning("Эндпоинт /users/whoami недоступен, пропускаю проверку токена")
                return
            raise
        if info:
            roles = info.get("roles") or []
            role_names = ", ".join(
                str(role.get("name") or role.get("id") or role)
                if isinstance(role, dict)
                else str(role)
                for role in roles[:5]
            )
            self.log.info(
                "Токен действителен: %s (%s)",
                info.get("login") or info.get("name") or "—",
                role_names or "роли не указаны",
            )

    # ------------------------------------------------------------------ #
    # Базовые запросы
    # ------------------------------------------------------------------ #

    def _query(
        self,
        name: str,
        sql: str,
        start: datetime,
        end: datetime,
        required: bool = False,
    ) -> list[dict[str, Any]]:
        period_from, period_to = inclusive_period(start, end)
        started = time.perf_counter()
        try:
            rows = self.client.search_events(
                sql, period_from, period_to, cluster_id=self.cluster_id
            )
        except (KumaAuthError, KumaConnectionError):
            raise
        except KumaError as exc:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self.stats.append(
                QueryStat(name=name, sql=sql, rows=0, duration_ms=duration_ms, ok=False, error=str(exc))
            )
            self.failed.add(name)
            if required:
                raise
            self.log.warning("Запрос '%s' не выполнен: %s", name, exc)
            return []
        duration_ms = int((time.perf_counter() - started) * 1000)
        self.stats.append(
            QueryStat(name=name, sql=sql, rows=len(rows), duration_ms=duration_ms, ok=True)
        )
        self.log.debug("Запрос '%s': %s строк за %s мс", name, len(rows), duration_ms)
        return rows

    def _collect_total(self, event_type: int, start: datetime, end: datetime) -> int:
        sql = f"SELECT count(ID) FROM `events` WHERE Type = {event_type}"
        rows = self._query("total", sql, start, end, required=True)
        return _count_from_rows(rows)

    def _collect_rules(
        self, event_type: int, start: datetime, end: datetime
    ) -> tuple[list[RuleCount], bool]:
        field = self.config.kuma.rule_field
        limit = int(self.config.kuma.max_rule_rows)
        sql = (
            f"SELECT count(ID), {field}, Severity FROM `events` "
            f"WHERE Type = {event_type} "
            f"GROUP BY {field}, Severity ORDER BY count(ID) DESC LIMIT {limit}"
        )
        rows = self._query("rules", sql, start, end)
        if "rules" in self.failed:
            # Некоторые сборки KUMA могут не разрешать Severity в GROUP BY.
            sql = (
                f"SELECT count(ID), {field} FROM `events` WHERE Type = {event_type} "
                f"GROUP BY {field} ORDER BY count(ID) DESC LIMIT {limit}"
            )
            rows = self._query("rules_simple", sql, start, end)
            if "rules_simple" not in self.failed:
                self.failed.discard("rules")

        rules: dict[str, RuleCount] = {}
        severity_by_name: dict[str, Counter[str]] = {}
        for row in rows:
            name = _field(row, field) or NO_NAME
            count = _count_value(row)
            severity = _field(row, "Severity")
            item = rules.get(name)
            if item is None:
                item = RuleCount(name=name, count=0)
                rules[name] = item
            item.count += count
            if severity:
                severity_by_name.setdefault(name, Counter())[severity] += count
        for name, counter in severity_by_name.items():
            rules[name].severity = counter.most_common(1)[0][0]
        ordered = sorted(rules.values(), key=lambda r: (-r.count, r.name.casefold()))
        truncated = len(rows) >= limit
        if truncated:
            self.warnings.append(
                f"Список правил ограничен {limit} строками: редкие правила могут отсутствовать."
            )
        return ordered, truncated

    def _collect_named(
        self,
        name: str,
        event_type: int,
        field: str,
        start: datetime,
        end: datetime,
    ) -> list[NamedCount]:
        sql = (
            f"SELECT count(ID), {field} FROM `events` WHERE Type = {event_type} "
            f"GROUP BY {field} ORDER BY count(ID) DESC"
        )
        rows = self._query(name, sql, start, end)
        return _named_from_rows(rows, field, drop_empty=True)

    def _collect_tenants(
        self, event_type: int, start: datetime, end: datetime
    ) -> list[NamedCount]:
        if not self.config.kuma.collect_tenants:
            return []
        rows = self._collect_named("tenants", event_type, "TenantID", start, end)
        if not rows:
            return []
        names: dict[str, str] = {}
        try:
            for tenant in self.client.get_tenants():
                names[str(tenant.get("id") or "")] = str(tenant.get("name") or "")
        except KumaError as exc:
            self.log.warning("Не удалось получить названия тенантов: %s", exc)
        for item in rows:
            item.meta["tenant_id"] = item.name
            if names.get(item.name):
                item.meta["name"] = names[item.name]
        return rows

    def _collect_entities(
        self, event_type: int, start: datetime, end: datetime
    ) -> dict[str, list[NamedCount]]:
        result: dict[str, list[NamedCount]] = {}
        limit = int(self.config.kuma.entity_limit)
        for field in self.config.kuma.entity_fields:
            sql = (
                f"SELECT count(ID), {field} FROM `events` WHERE Type = {event_type} "
                f"GROUP BY {field} ORDER BY count(ID) DESC LIMIT {limit}"
            )
            rows = self._query(f"entity:{field}", sql, start, end)
            result[field] = _named_from_rows(rows, field, drop_empty=True)
        return result

    def _collect_health(
        self, start: datetime, end: datetime
    ) -> tuple[int | None, int | None]:
        if not self.config.kuma.collect_health:
            return None, None
        base = self._count_query(
            "health_base",
            "SELECT count(ID) FROM `events` WHERE Type = 1",
            start,
            end,
        )
        aggregated = self._count_query(
            "health_aggregated",
            "SELECT count(ID) FROM `events` WHERE Type = 2",
            start,
            end,
        )
        return base, aggregated

    def _count_query(
        self, name: str, sql: str, start: datetime, end: datetime
    ) -> int | None:
        rows = self._query(name, sql, start, end)
        if name in self.failed:
            return None
        return _count_from_rows(rows)

    # ------------------------------------------------------------------ #
    # Сырые события
    # ------------------------------------------------------------------ #

    def _collect_raw(
        self, event_type: int, start: datetime, end: datetime
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not self.config.kuma.collect_raw_events:
            return {"enabled": False}, []
        limit = int(self.config.kuma.raw_events_limit)
        fields = ", ".join(self.config.kuma.raw_fields)
        sql = (
            f"SELECT {fields} FROM `events` WHERE Type = {event_type} "
            f"ORDER BY Timestamp DESC LIMIT {limit + 1}"
        )
        rows = self._query("raw_events", sql, start, end)
        if "raw_events" in self.failed:
            self.warnings.append(
                "Выборка сырых событий недоступна — часть деталей отчёта будет пустой."
            )
            return {"enabled": False}, []
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
            self.warnings.append(
                f"Сырые события ограничены {limit} записями (последние по времени). "
                "Детали по правилам и уникальным значениям — по выборке."
            )
        summary = self._summarize_raw(rows, truncated)
        self.log.info(
            "Сырые события: %s из %s%s",
            len(rows),
            limit,
            " (усечено)" if truncated else "",
        )
        return summary, rows

    def _summarize_raw(
        self, events: list[dict[str, Any]], truncated: bool
    ) -> dict[str, Any]:
        rule_field = self.config.kuma.rule_field
        rule_info: dict[str, dict[str, Any]] = {}
        unique_sets: dict[str, set[str]] = {
            field: set()
            for field in (
                "SourceAddress",
                "SourceUserName",
                "DestinationAddress",
                "DestinationUserName",
                "DeviceHostName",
                "SourceHostName",
            )
        }
        entity_counters: dict[str, Counter[str]] = {
            field: Counter() for field in self.config.kuma.entity_fields
        }
        severity_counter: Counter[str] = Counter()
        tactic_counter: Counter[str] = Counter()
        tenant_counter: Counter[str] = Counter()
        pairs: Counter[tuple[str, str]] = Counter()
        rule_hosts: Counter[tuple[str, str]] = Counter()
        invalid_timestamps = 0

        for event in events:
            name = (
                _field(event, rule_field)
                or _field(event, "CorrelationRuleName")
                or _field(event, "Name")
                or NO_NAME
            )
            info = rule_info.setdefault(
                name,
                {
                    "events": 0,
                    "sources": set(),
                    "severities": Counter(),
                    "ids": Counter(),
                    "tactics": Counter(),
                    "first": None,
                    "last": None,
                },
            )
            info["events"] += 1

            timestamp = parse_dt(event.get("Timestamp")) or parse_dt(
                event.get("DeviceReceiptTime")
            )
            if timestamp is None:
                invalid_timestamps += 1
            else:
                if info["first"] is None or timestamp < info["first"]:
                    info["first"] = timestamp
                if info["last"] is None or timestamp > info["last"]:
                    info["last"] = timestamp

            source = _field(event, "SourceAddress")
            if source:
                info["sources"].add(source)
                pairs[(name, source)] += 1
            host = _field(event, "DeviceHostName") or _field(event, "SourceHostName")
            if host:
                rule_hosts[(name, host)] += 1

            severity = _field(event, "Severity")
            if severity:
                info["severities"][severity] += 1
                severity_counter[severity] += 1
            rule_id = _field(event, "CorrelationRuleID")
            if rule_id:
                info["ids"][rule_id] += 1
            tactic = _field(event, "Tactic")
            if tactic:
                info["tactics"][tactic] += 1
                tactic_counter[tactic] += 1
            tenant = _field(event, "TenantID")
            if tenant:
                tenant_counter[tenant] += 1

            for field, bucket in unique_sets.items():
                value = _field(event, field)
                if value:
                    bucket.add(value)
            for field, counter in entity_counters.items():
                value = _field(event, field)
                if value:
                    counter[value] += 1

        rules: dict[str, dict[str, Any]] = {}
        for name, info in rule_info.items():
            first = info["first"]
            last = info["last"]
            rules[name] = {
                "events": int(info["events"]),
                "severity": _top_key(info["severities"]),
                "rule_id": _top_key(info["ids"]),
                "sources": len(info["sources"]),
                "first": to_rfc3339(first) if first and not truncated else "",
                "last": to_rfc3339(last) if last else "",
                "tactics": [key for key, _ in info["tactics"].most_common(5)],
            }

        return {
            "enabled": True,
            "events": len(events),
            "truncated": truncated,
            "invalid_timestamps": invalid_timestamps,
            "unique": {field: len(bucket) for field, bucket in unique_sets.items()},
            "rules": rules,
            "severity": [
                {"name": name, "count": count}
                for name, count in severity_counter.most_common()
            ],
            "tactics": [
                {"name": name, "count": count}
                for name, count in tactic_counter.most_common(30)
            ],
            "tenants": [
                {"name": name, "count": count}
                for name, count in tenant_counter.most_common(50)
            ],
            "entities": {
                field: [
                    {"name": name, "count": count}
                    for name, count in counter.most_common(self.config.kuma.entity_limit)
                ]
                for field, counter in entity_counters.items()
            },
            "pairs": [
                {"rule": rule, "source": source, "count": count}
                for (rule, source), count in pairs.most_common(30)
            ],
            "rule_hosts": [
                {"rule": rule, "host": host, "count": count}
                for (rule, host), count in rule_hosts.most_common(30)
            ],
        }

    def _merge_rule_details(
        self,
        rules: list[RuleCount],
        rules_truncated: bool,
        raw: dict[str, Any],
    ) -> tuple[list[RuleCount], bool]:
        raw_rules = raw.get("rules") or {}
        raw_truncated = bool(raw.get("truncated"))
        needs_rebuild = not rules or rules_truncated
        if (
            needs_rebuild
            and raw_rules
            and not raw_truncated
            and raw.get("enabled")
        ):
            rebuilt = [
                RuleCount(
                    name=name,
                    count=int(info.get("events") or 0),
                    severity=str(info.get("severity") or ""),
                    rule_id=str(info.get("rule_id") or ""),
                    unique_sources=_optional_int(info.get("sources")),
                    first_seen=str(info.get("first") or ""),
                    last_seen=str(info.get("last") or ""),
                )
                for name, info in raw_rules.items()
            ]
            rebuilt.sort(key=lambda r: (-r.count, r.name.casefold()))
            self.log.info("Список правил восстановлен по полной выборке сырых событий")
            rules = rebuilt
            rules_truncated = False
            self.warnings = [
                warning
                for warning in self.warnings
                if not warning.startswith("Список правил ограничен")
            ]
            if "rules" in self.failed or "rules_simple" in self.failed:
                self.warnings.append(
                    "Агрегированный запрос по правилам не удался; данные взяты из сырых событий."
                )
            return rules, rules_truncated

        if raw.get("enabled") and raw_rules:
            for rule in rules:
                info = raw_rules.get(rule.name)
                if not info:
                    continue
                if not rule.severity:
                    rule.severity = str(info.get("severity") or "")
                if not rule.rule_id:
                    rule.rule_id = str(info.get("rule_id") or "")
                rule.unique_sources = _optional_int(info.get("sources"))
                rule.last_seen = str(info.get("last") or "")
                if not raw_truncated:
                    rule.first_seen = str(info.get("first") or "")
        return rules, rules_truncated

    def _merge_entities(
        self, entities: dict[str, list[NamedCount]], raw: dict[str, Any]
    ) -> dict[str, list[NamedCount]]:
        raw_entities = raw.get("entities") or {}
        for field in self.config.kuma.entity_fields:
            if entities.get(field):
                continue
            if f"entity:{field}" in self.failed and raw_entities.get(field):
                entities[field] = [NamedCount.from_dict(x) for x in raw_entities[field]]
                self.failed.discard(f"entity:{field}")
                self.warnings.append(
                    f"Разрез {field} построен по выборке сырых событий."
                )
        return entities

    # ------------------------------------------------------------------ #
    # Почасовая динамика
    # ------------------------------------------------------------------ #

    def _collect_hourly(
        self,
        event_type: int,
        start: datetime,
        end: datetime,
        raw_events: list[dict[str, Any]],
        raw: dict[str, Any],
    ) -> tuple[list[int], list[str], list[list[RuleCount]] | None, bool]:
        if not self.config.kuma.hourly_breakdown:
            return [], [], None, False

        slices = hour_slices(start, end)
        field = self.config.kuma.rule_field
        limit = int(self.config.kuma.hourly_rules_limit)
        totals: list[int] = []
        rule_buckets: list[list[RuleCount]] = []
        failed_slices = 0
        truncated = False

        for index, (slice_start, slice_end) in enumerate(slices):
            sql = (
                f"SELECT count(ID), {field} FROM `events` WHERE Type = {event_type} "
                f"GROUP BY {field} ORDER BY count(ID) DESC LIMIT {limit}"
            )
            name = f"hourly_{index:02d}"
            rows = self._query(name, sql, slice_start, slice_end)
            if name in self.failed:
                failed_slices += 1
                totals.append(0)
                rule_buckets.append([])
                continue
            if len(rows) >= limit:
                truncated = True
            pairs = _named_from_rows(rows, field, drop_empty=False)
            totals.append(sum(item.count for item in pairs))
            rule_buckets.append(
                [RuleCount(name=item.name, count=item.count) for item in pairs]
            )

        partial = failed_slices > 0 or truncated
        if failed_slices:
            self.warnings.append(
                f"Почасовая детализация неполная: {failed_slices} из {len(slices)} срезов не получены."
            )
        if truncated:
            self.warnings.append(
                f"В почасовых срезах учитываются не более {limit} правил на час."
            )

        labels = _unique_labels(
            [hour_label(slice_start.astimezone(self.tz)) for slice_start, _ in slices]
        )

        if failed_slices and raw_events and not raw.get("truncated"):
            rebuilt_totals, rebuilt_buckets = self._hourly_from_raw(
                raw_events, slices, field
            )
            for index in range(len(slices)):
                self.failed.discard(f"hourly_{index:02d}")
            self.warnings = [
                warning
                for warning in self.warnings
                if "Почасовая детализация" not in warning
                and "В почасовых срезах" not in warning
            ]
            self.warnings.append(
                "Почасовая детализация восстановлена по сырым событиям."
            )
            return rebuilt_totals, labels, rebuilt_buckets, False

        if failed_slices == len(slices):
            return [], [], None, True

        return totals, labels, rule_buckets, partial

    def _hourly_from_raw(
        self,
        events: list[dict[str, Any]],
        slices: list[tuple[datetime, datetime]],
        field: str,
    ) -> tuple[list[int], list[list[RuleCount]]]:
        totals = [0] * len(slices)
        buckets: list[Counter[str]] = [Counter() for _ in slices]
        for event in events:
            timestamp = parse_dt(event.get("Timestamp")) or parse_dt(
                event.get("DeviceReceiptTime")
            )
            if timestamp is None:
                continue
            index = None
            for position, (slice_start, slice_end) in enumerate(slices):
                if slice_start <= timestamp < slice_end:
                    index = position
                    break
            if index is None:
                continue
            name = (
                _field(event, field)
                or _field(event, "CorrelationRuleName")
                or _field(event, "Name")
                or NO_NAME
            )
            totals[index] += 1
            buckets[index][name] += 1
        rule_buckets = [
            [
                RuleCount(name=name, count=count)
                for name, count in counter.most_common()
            ]
            for counter in buckets
        ]
        return totals, rule_buckets

    # ------------------------------------------------------------------ #
    # Инвентарь правил
    # ------------------------------------------------------------------ #

    def _collect_inventory(self) -> list[NamedCount]:
        if not self.config.kuma.collect_rule_inventory:
            return []
        try:
            resources = self.client.get_correlation_rules()
        except KumaError as exc:
            self.log.warning("Не удалось получить список правил корреляции: %s", exc)
            self.warnings.append(
                "Инвентарь правил корреляции недоступен — раздел о молчащих правилах пропущен."
            )
            return []
        inventory = [
            NamedCount(
                name=str(item.get("name") or "").strip(),
                count=0,
                meta={
                    "id": str(item.get("id") or ""),
                    "tenant": str(item.get("tenantName") or item.get("tenantID") or ""),
                },
            )
            for item in resources
            if str(item.get("name") or "").strip()
        ]
        inventory.sort(key=lambda item: item.name.casefold())
        self.log.info("Получен инвентарь правил корреляции: %s", len(inventory))
        return inventory

    # ------------------------------------------------------------------ #
    # Предупреждения
    # ------------------------------------------------------------------ #

    def _collect_warnings(
        self,
        total: int,
        rules: list[RuleCount],
        rules_truncated: bool,
        base_events: int | None,
        raw_summary: dict[str, Any],
    ) -> None:
        if self.failed:
            failed_names = ", ".join(sorted(self.failed))
            self.warnings.append(
                f"Часть запросов не выполнена ({len(self.failed)}): {failed_names}. "
                "Подробности — в приложении отчёта."
            )
        if total == 0:
            self.warnings.append("За период не найдено ни одной сработки правил корреляции.")
        elif not rules:
            self.warnings.append(
                "Сработки есть, но получить названия правил не удалось."
            )
        elif rules_truncated:
            pass  # предупреждение добавлено при сборе
        elif rules:
            sum_rules = sum(rule.count for rule in rules)
            if total > 0 and sum_rules < total * 0.995:
                self.warnings.append(
                    f"Сумма сработок по правилам ({sum_rules}) меньше общего числа ({total}). "
                    "Возможны события без названия или усечение выборки."
                )

        no_name = next((r for r in rules if r.name == NO_NAME), None)
        if no_name and no_name.count:
            self.warnings.append(
                f"{no_name.count} сработок без названия правила — проверьте настройки правил."
            )

        if base_events == 0:
            self.warnings.append(
                "Базовых событий (Type=1) за период нет. Проверьте сбор событий "
                "и работу коррелятора: сработки могли быть получены не из базового потока."
            )

        invalid_ts = int(raw_summary.get("invalid_timestamps") or 0)
        if invalid_ts:
            self.warnings.append(
                f"Не удалось разобрать метки времени у {invalid_ts} событий выборки."
            )


# --------------------------------------------------------------------------- #
# Парсинг строк ответа
# --------------------------------------------------------------------------- #

def _count_value(row: dict[str, Any]) -> int:
    for key, value in row.items():
        if str(key).lower().startswith("count("):
            return _to_int(value)
    for key in ("count", "cnt", "total"):
        if key in row:
            return _to_int(row[key])
    numeric = [
        value
        for value in row.values()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if len(numeric) == 1:
        return _to_int(numeric[0])
    return 0


def _count_from_rows(rows: list[dict[str, Any]]) -> int:
    return sum(_count_value(row) for row in rows)


def _named_from_rows(
    rows: list[dict[str, Any]], field: str, drop_empty: bool
) -> list[NamedCount]:
    counter: Counter[str] = Counter()
    for row in rows:
        name = _field(row, field)
        if drop_empty and not name:
            continue
        counter[name or NO_NAME] += _count_value(row)
    return [
        NamedCount(name=name, count=count)
        for name, count in sorted(counter.items(), key=lambda x: (-x[1], x[0].casefold()))
    ]


def _field(row: dict[str, Any], field: str) -> str:
    if field in row:
        value = row.get(field)
    else:
        value = None
        lowered = field.casefold()
        for key, candidate in row.items():
            if str(key).casefold() == lowered:
                value = candidate
                break
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    return str(value).strip()


def _to_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _top_key(counter: Counter[str]) -> str:
    return counter.most_common(1)[0][0] if counter else ""


def _unique_labels(labels: list[str]) -> list[str]:
    seen: Counter[str] = Counter()
    result: list[str] = []
    for label in labels:
        seen[label] += 1
        if seen[label] == 1:
            result.append(label)
        else:
            result.append(f"{label}*")
    return result


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result
