"""Историческая база, аномалии и производные показатели."""
from __future__ import annotations

from datetime import date, timedelta

from .config import Config
from .models import Baseline, DailyData, NamedCount, RuleRow, RuleStats
from .storage import HistoryStore
from .utils import mean, median, pct_delta, pstdev

FLAG_NEW = "новое"
FLAG_GROWTH = "рост"
FLAG_DROP = "падение"
FLAG_NOISE = "шум"
FLAG_SILENT = "молчит"


def load_baseline(store: HistoryStore | None, config: Config, data: DailyData) -> Baseline:
    cfg = config.baseline
    totals_pairs: list[tuple[str, int]] = []
    rule_counts_by_day: dict[str, dict[str, int]] = {}
    if store is not None:
        totals_pairs = store.totals(data.cluster_id, before_day=data.day, window_days=cfg.days)
        rule_counts_by_day = store.rule_counts(
            data.cluster_id, before_day=data.day, window_days=cfg.days
        )

    days = [day for day, _ in totals_pairs]
    totals = dict(totals_pairs)
    values = [totals[day] for day in days]
    days_available = len(days)
    enough = days_available >= cfg.min_days

    mean_total = mean([float(v) for v in values])
    std_total = pstdev([float(v) for v in values])
    mean7 = mean([float(v) for v in values[-7:]])
    prev_total = values[-1] if values else None

    total_z = _zscore(float(data.total), mean_total, std_total) if mean_total is not None else None
    total_delta = pct_delta(data.total, mean_total)

    return Baseline(
        days_available=days_available,
        window_days=cfg.days,
        enough_data=enough,
        days=days,
        totals=totals,
        prev_total=prev_total,
        mean=mean_total,
        std=std_total,
        mean7=mean7,
        rule_stats=_build_rule_stats(rule_counts_by_day, days),
        total_z=total_z,
        total_delta_pct=total_delta,
    )


def _build_rule_stats(
    by_day: dict[str, dict[str, int]], days: list[str]
) -> dict[str, RuleStats]:
    if not days:
        return {}
    maps = [by_day.get(day, {}) for day in days]
    names: set[str] = set()
    for mapping in maps:
        names.update(mapping)
    result: dict[str, RuleStats] = {}
    for name in names:
        values = [int(mapping.get(name, 0)) for mapping in maps]
        last_seen = ""
        for day, value in zip(reversed(days), reversed(values)):
            if value > 0:
                last_seen = day
                break
        result[name] = RuleStats(
            mean=mean([float(v) for v in values]) or 0.0,
            std=pstdev([float(v) for v in values]) or 0.0,
            median=median([float(v) for v in values]) or 0.0,
            max_seen=max(values),
            days_seen=sum(1 for v in values if v > 0),
            mean7=mean([float(v) for v in values[-7:]]) or 0.0,
            prev_count=values[-1],
            last_seen_day=last_seen,
        )
    return result


def build_rows(data: DailyData, baseline: Baseline, config: Config) -> list[RuleRow]:
    cfg = config.baseline
    total = max(0, int(data.total))
    rows: list[RuleRow] = []
    for rule in data.rules:
        stats = baseline.rule_stats.get(rule.name)
        count = int(rule.count)
        share = (count / total * 100.0) if total else 0.0

        base_mean = stats.mean if stats else (0.0 if baseline.days_available else None)
        mean7 = stats.mean7 if stats else (0.0 if baseline.days_available else None)
        std = stats.std if stats else (0.0 if baseline.days_available else None)
        prev = stats.prev_count if stats else (0 if baseline.days_available else None)
        days_seen = stats.days_seen if stats else 0
        max_seen = stats.max_seen if stats else (0 if baseline.days_available else None)
        med = stats.median if stats else (0.0 if baseline.days_available else None)
        last_seen_day = stats.last_seen_day if stats else ""

        delta = pct_delta(count, base_mean) if base_mean not in (None, 0) else None
        z = (
            _zscore(float(count), base_mean, std)
            if cfg is not None and baseline.enough_data and stats is not None
            else None
        )

        flags: list[str] = []
        if baseline.days_available > 0 and stats is None and count > 0:
            flags.append(FLAG_NEW)

        if baseline.enough_data and stats is not None:
            if (
                count >= cfg.min_events
                and z is not None
                and z >= cfg.z_threshold
                and count - base_mean >= cfg.min_delta
            ):
                flags.append(FLAG_GROWTH)
            if (
                base_mean >= cfg.min_events
                and z is not None
                and z <= -cfg.z_threshold
                and base_mean - count >= cfg.min_delta
            ):
                flags.append(FLAG_DROP)

        if (
            not any(flag in flags for flag in (FLAG_NEW, FLAG_GROWTH, FLAG_DROP))
            and baseline.enough_data
            and count >= cfg.noise_min_events
            and days_seen >= min(7, baseline.days_available)
            and delta is not None
            and abs(delta) <= cfg.noise_max_delta_pct
        ):
            flags.append(FLAG_NOISE)

        rows.append(
            RuleRow(
                name=rule.name,
                count=count,
                share_pct=round(share, 2),
                severity=rule.severity or "",
                rule_id=rule.rule_id or "",
                prev_count=prev,
                mean7=mean7,
                mean=base_mean,
                std=std,
                median=med,
                max_seen=max_seen,
                days_seen=days_seen,
                delta_pct=delta,
                z=z,
                flags=flags,
                unique_sources=rule.unique_sources,
                first_seen=rule.first_seen,
                last_seen=rule.last_seen,
                last_seen_day=last_seen_day,
            )
        )

    rows.sort(key=lambda row: (-row.count, row.name.casefold()))
    return rows


def select_anomalies(rows: list[RuleRow]) -> list[RuleRow]:
    interesting = {FLAG_NEW, FLAG_GROWTH, FLAG_DROP}
    result = [row for row in rows if interesting.intersection(row.flags)]
    result.sort(
        key=lambda row: (
            0 if FLAG_NEW in row.flags else 1,
            -abs(row.z) if row.z is not None else 0,
            -row.count,
        )
    )
    return result


def total_anomaly(baseline: Baseline, config: Config) -> dict[str, object]:
    cfg = config.baseline
    if baseline.mean is None or not baseline.enough_data or baseline.total_z is None:
        return {"is_anomaly": False, "z": baseline.total_z, "delta_pct": baseline.total_delta_pct}
    is_anomaly = (
        abs(baseline.total_z) >= cfg.z_threshold
        and abs(baseline.total_delta_pct or 0) >= cfg.total_min_delta_pct
    )
    return {
        "is_anomaly": is_anomaly,
        "z": baseline.total_z,
        "delta_pct": baseline.total_delta_pct,
        "direction": "рост" if (baseline.total_delta_pct or 0) > 0 else "падение",
    }


def silent_rules(
    data: DailyData, baseline: Baseline, config: Config
) -> list[RuleRow]:
    """Правила, которые раньше срабатывали, а сегодня — нет."""
    cfg = config.baseline
    if not baseline.enough_data or data.rules_truncated:
        return []
    today = {rule.name for rule in data.rules}
    result: list[RuleRow] = []
    for name, stats in baseline.rule_stats.items():
        if name in today or stats.mean < cfg.min_events:
            continue
        z = _zscore(0.0, stats.mean, stats.std)
        result.append(
            RuleRow(
                name=name,
                count=0,
                share_pct=0.0,
                mean=stats.mean,
                mean7=stats.mean7,
                std=stats.std,
                median=stats.median,
                max_seen=stats.max_seen,
                days_seen=stats.days_seen,
                delta_pct=-100.0 if stats.mean else None,
                z=z,
                flags=[FLAG_SILENT],
                last_seen_day=stats.last_seen_day,
            )
        )
    result.sort(key=lambda row: (-(row.mean or 0), row.name.casefold()))
    return result


def inventory_analysis(
    data: DailyData, baseline: Baseline, config: Config
) -> dict[str, list[NamedCount]] | None:
    """Сопоставление инвентаря правил с фактическими сработками.

    Важно: список ресурсов KUMA не содержит признака «включено/выключено»,
    поэтому результат носит информационный характер.
    """
    if not data.inventory:
        return None
    today = {rule.name for rule in data.rules}
    cutoff = (date.fromisoformat(data.day) - timedelta(days=config.baseline.new_rule_days)).isoformat()

    not_today: list[NamedCount] = []
    long_silent: list[NamedCount] = []
    for item in data.inventory:
        if item.name not in today:
            not_today.append(item)
        if baseline.days_available == 0:
            continue
        stats = baseline.rule_stats.get(item.name)
        if stats is None or (stats.last_seen_day and stats.last_seen_day < cutoff):
            long_silent.append(item)

    not_today.sort(key=lambda x: x.name.casefold())
    long_silent.sort(key=lambda x: x.name.casefold())
    return {"not_today": not_today, "long_silent": long_silent}


def history_note(baseline: Baseline, config: Config) -> str:
    if baseline.days_available == 0:
        return (
            "Истории ещё нет: это первый отчёт по кластеру. "
            "Сравнение и аномалии появятся после нескольких ежедневных запусков."
        )
    if not baseline.enough_data:
        return (
            f"Истории недостаточно для аномалий: {baseline.days_available} из "
            f"{config.baseline.min_days} требуемых дней. Показаны средние и динамика."
        )
    return ""


def _zscore(value: float, center: float | None, spread: float | None) -> float | None:
    if center is None:
        return None
    if not spread:
        if value > center:
            return 99.0
        if value < center:
            return -99.0
        return 0.0
    return (value - center) / spread
