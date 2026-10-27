"""Формирование HTML-отчёта, CSV/JSON и архива отчётов."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import quote

from . import __version__
from .charts import PALETTE, donut, hbar, heatmap, line_chart
from .config import Config
from .models import Baseline, DailyData, NamedCount, RuleRow
from .utils import (
    ensure_dir,
    esc,
    fmt_duration,
    fmt_float,
    fmt_int,
    fmt_pct,
    load_timezone,
    parse_dt,
    read_json,
    severity_color,
    severity_label,
    truncate,
    write_csv,
    write_json,
)

ENTITY_TITLES = {
    "SourceAddress": "Источники (SourceAddress)",
    "SourceUserName": "Учётные записи источников (SourceUserName)",
    "DestinationAddress": "Назначения (DestinationAddress)",
    "DestinationUserName": "Учётные записи назначения",
    "DeviceHostName": "Хосты устройств (DeviceHostName)",
    "SourceHostName": "Хосты источников (SourceHostName)",
}

FLAG_CLASSES = {
    "новое": "flag-new",
    "рост": "flag-up",
    "падение": "flag-down",
    "шум": "flag-noise",
    "молчит": "flag-silent",
}


@dataclass
class ReportResult:
    day_dir: Path
    html_path: Path
    summary_path: Path
    snapshot_path: Path | None
    summary: dict[str, Any]
    files: list[Path]


def build_report(
    config: Config,
    data: DailyData,
    baseline: Baseline,
    rows: list[RuleRow],
    anomalies: list[RuleRow],
    silent: list[RuleRow],
    inventory_info: dict[str, list[NamedCount]] | None,
    *,
    write_snapshot: bool = True,
    out_dir: str | Path | None = None,
    out_html: str | Path | None = None,
    write_index: bool = True,
    logger: logging.Logger | None = None,
) -> ReportResult:
    log = logger or logging.getLogger("kuma_stats")
    tz, tz_warning = load_timezone(
        config.report.timezone, config.report.utc_offset_hours
    )
    if tz_warning:
        data.warnings.append(tz_warning)

    if out_html:
        html_path = Path(out_html)
        if html_path.exists() and html_path.is_dir():
            html_path = html_path / "report.html"
        day_dir = html_path.parent
    elif out_dir:
        day_dir = Path(out_dir)
        html_path = day_dir / "report.html"
    else:
        day_dir = Path(config.report.output_dir) / data.day
        html_path = day_dir / "report.html"
    ensure_dir(day_dir)
    ensure_dir(html_path.parent)

    snapshot_path: Path | None = None
    if write_snapshot:
        snapshot_path = day_dir / "snapshot.json"
        write_json(snapshot_path, data.to_dict())

    files = _write_csvs(day_dir, data, rows, anomalies, silent, inventory_info)
    summary = _build_summary(config, data, baseline, rows, anomalies, silent)
    summary_path = day_dir / "summary.json"
    write_json(summary_path, summary)
    files.extend([summary_path])

    html = _render_html(
        config, data, baseline, rows, anomalies, silent, inventory_info, tz
    )
    html_path.write_text(html, encoding="utf-8")
    files.append(html_path)

    if write_index:
        try:
            _write_index(config)
        except OSError as exc:
            log.warning("Не удалось обновить архив отчётов: %s", exc)

    log.info("Отчёт сохранён: %s", html_path)
    return ReportResult(
        day_dir=day_dir,
        html_path=html_path,
        summary_path=summary_path,
        snapshot_path=snapshot_path,
        summary=summary,
        files=files,
    )


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def _render_html(
    config: Config,
    data: DailyData,
    baseline: Baseline,
    rows: list[RuleRow],
    anomalies: list[RuleRow],
    silent: list[RuleRow],
    inventory_info: dict[str, list[NamedCount]] | None,
    tz,
) -> str:
    title = f"{config.report.title} · {_local_date(data.day)}"
    parts: list[str] = []
    parts.append(_hero(config, data, tz))
    parts.append(_kpis(config, data, baseline, anomalies))
    alerts = _alerts(config, data, baseline)
    if alerts:
        parts.append(_card("На что обратить внимание", alerts, subtitle="Предупреждения о качестве данных и отклонениях"))
    parts.append(_anomalies_card(config, data, baseline, anomalies))
    parts.append(_top_rules_card(config, data, rows))
    parts.append(_hourly_card(config, data))
    parts.append(_severity_tactics(config, data))
    parts.append(_entities_card(config, data))
    if data.tenants:
        parts.append(_tenants_card(data))
    parts.append(_silent_card(config, data, baseline, silent))
    if inventory_info is not None:
        parts.append(_inventory_card(config, data, baseline, inventory_info))
    parts.append(_history_card(config, baseline))
    parts.append(_appendix(config, data, rows))

    body = f"""
<div class="page">
{''.join(parts)}
<div class="footer">
  {esc(config.report.title)} · сформировано автоматически {esc(_local_time(data.generated_at, tz, seconds=True))}<br>
  Инструмент kuma-daily-stats {esc(__version__)} · источник: Kaspersky KUMA REST API v3 · события Type={data.event_type}<br>
  Отчёт может содержать чувствительные данные (IP-адреса, учётные записи). Храните и передавайте его соответствующим образом.
</div>
</div>
"""
    return _page(title, body)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="kuma-daily-stats {esc(__version__)}">
<title>{esc(title)}</title>
<style>{_CSS}</style>
</head>
<body>
{body}
</body>
</html>
"""


def _hero(config: Config, data: DailyData, tz) -> str:
    cluster = data.cluster_name or data.cluster_id or "по умолчанию"
    cluster_extra = (
        f" ({data.cluster_id})" if data.cluster_name and data.cluster_id else ""
    )
    period = (
        f"{esc(_local_time(data.period_from, tz))} — "
        f"{esc(_local_time(data.period_to, tz))}"
    )
    return f"""
<header class="hero">
  <div class="eyebrow">KUMA · ежедневная аналитика</div>
  <h1>{esc(config.report.title)}</h1>
  <div class="sub">Сработки правил корреляции за {esc(_local_date(data.day))}. Сравнение с историей, аномалии, динамика и разрезы.</div>
  <div class="meta">
    <span>Период: <b>{period}</b></span>
    <span>Часовой пояс: <b>{esc(data.timezone)}</b></span>
    <span>Кластер: <b>{esc(cluster)}{esc(cluster_extra)}</b></span>
    <span>Сформирован: <b>{esc(_local_time(data.generated_at, tz, seconds=True))}</b></span>
  </div>
</header>
"""


def _kpis(
    config: Config,
    data: DailyData,
    baseline: Baseline,
    anomalies: list[RuleRow],
) -> str:
    delta = baseline.total_delta_pct
    total_tone = "warn" if delta is not None and delta >= 20 else ""
    if delta is not None:
        direction_class = "up" if delta > 0 else "down" if delta < 0 else ""
        class_attr = f' class="{direction_class}"' if direction_class else ""
        total_hint = (
            f"<span{class_attr}>{esc(fmt_pct(delta, signed=True))}</span>"
            " к среднему"
        )
    else:
        total_hint = "история накапливается"
    mean_value = fmt_int(baseline.mean) if baseline.mean is not None else "—"
    mean_hint = (
        f"за {baseline.days_available} дн. истории"
        if baseline.days_available
        else "нет истории"
    )
    top_share = rows_top_share(data)
    rules_hint = f"топ-1: {esc(fmt_pct(top_share))}" if top_share else "правила не получены"
    raw = data.raw if isinstance(data.raw, dict) else {}
    unique = raw.get("unique") if isinstance(raw.get("unique"), dict) else {}
    sources = unique.get("SourceAddress")
    sources_hint = "по выборке" if raw.get("truncated") else "уникальных SourceAddress"
    new_count = sum(1 for row in anomalies if "новое" in row.flags)
    anomalies_tone = "warn" if anomalies else ""
    peak_label = "—"
    peak_hint = "нет почасовых данных"
    if data.hourly:
        index = max(range(len(data.hourly)), key=lambda i: data.hourly[i])
        peak_label = data.hourly_labels[index] if index < len(data.hourly_labels) else "—"
        peak_hint = f"{fmt_int(data.hourly[index])} сработок"
    base_value = fmt_int(data.base_events) if data.base_events is not None else "—"
    base_tone = "warn" if data.base_events == 0 else ""
    base_hint = "Type=1, базовые события" if data.base_events is not None else "не собирались"

    cards = [
        _kpi("Сработок за сутки", fmt_int(data.total), total_hint, total_tone),
        _kpi("Среднее в сутки", mean_value, mean_hint),
        _kpi("Уникальных правил", fmt_int(len(data.rules)), rules_hint),
        _kpi("Источников", fmt_int(sources) if sources is not None else "—", sources_hint),
        _kpi(
            "Аномалий",
            fmt_int(len(anomalies)),
            f"новых правил: {fmt_int(new_count)}",
            anomalies_tone,
        ),
        _kpi("Новых правил", fmt_int(new_count), "не встречались в истории"),
        _kpi("Пиковый час", esc(peak_label), peak_hint),
        _kpi("Базовых событий", base_value, base_hint, base_tone),
    ]
    return '<section class="kpis">' + "".join(cards) + "</section>"


def _kpi(label: str, value: str, hint: str, tone: str = "") -> str:
    return (
        f'<div class="kpi {tone}"><div class="label">{esc(label)}</div>'
        f'<div class="value">{value}</div><div class="hint">{hint}</div></div>'
    )


def _alerts(config: Config, data: DailyData, baseline: Baseline) -> str:
    items: list[str] = []
    for warning in data.warnings:
        items.append(f'<li>{esc(warning)}</li>')
    note = _history_note(config, baseline)
    if note:
        items.append(f'<li class="info">{esc(note)}</li>')
    if baseline.enough_data and baseline.total_z is not None:
        if abs(baseline.total_z) >= config.baseline.z_threshold and abs(
            baseline.total_delta_pct or 0
        ) >= config.baseline.total_min_delta_pct:
            direction = "вырос" if (baseline.total_delta_pct or 0) > 0 else "снизился"
            items.append(
                f'<li class="danger">Общий объём сработок {direction} на '
                f"{esc(fmt_pct(abs(baseline.total_delta_pct or 0)))} относительно среднего "
                f"(z = {esc(fmt_float(baseline.total_z))}).</li>"
            )
    if not items:
        return ""
    return '<ul class="alerts">' + "".join(items) + "</ul>"


def _anomalies_card(
    config: Config, data: DailyData, baseline: Baseline, anomalies: list[RuleRow]
) -> str:
    subtitle = (
        f"Сравнение с {baseline.days_available} дн. истории · z ≥ {fmt_float(config.baseline.z_threshold)} "
        f"· минимум {fmt_int(config.baseline.min_events)} сработок"
    )
    if not baseline.enough_data:
        body = (
            '<p class="muted">Аномалии не рассчитывались: недостаточно истории. '
            f"Нужно минимум {config.baseline.min_days} дней, доступно {baseline.days_available}.</p>"
        )
    elif not anomalies:
        body = (
            '<p class="muted">Значимых отклонений от истории не обнаружено. '
            "Новые правила и резкие изменения объёма появятся здесь.</p>"
        )
    else:
        shown = anomalies[:100]
        table_rows = []
        for index, row in enumerate(shown, start=1):
            table_rows.append(
                [
                    str(index),
                    _rule_cell(row),
                    f'<span class="num">{fmt_int(row.count)}</span>',
                    f'<span class="num">{fmt_int(row.mean)}</span>',
                    f'<span class="num">{fmt_int(row.max_seen)}</span>',
                    _delta_html(row.delta_pct),
                    _z_html(row.z, config.baseline.z_threshold),
                    f'<span class="num">{fmt_int(row.days_seen)}</span>',
                    _flags_html(row.flags),
                ]
            )
        body = _table(
            ["#", "Правило", "Сработок", "Среднее", "Макс.", "Δ", "z", "Дней", "Флаги"],
            table_rows,
            aligns=["left", "left", "right", "right", "right", "right", "right", "right", "left"],
            table_class="rules-table",
        )
        if len(anomalies) > len(shown):
            body += f'<p class="note">Показаны первые {len(shown)} из {len(anomalies)}. Полный список — в CSV.</p>'
    return _card(
        "Аномалии и новые правила",
        body,
        subtitle=subtitle,
        extra_class="anomalies",
    )


def _top_rules_card(config: Config, data: DailyData, rows: list[RuleRow]) -> str:
    if not rows:
        return _card(
            "Топ правил",
            '<p class="muted">За период не удалось получить сработки по правилам.</p>',
            subtitle=f"Всего сработок: {fmt_int(data.total)}",
        )
    top_n = min(config.report.top_n, len(rows))
    chart_items = [(row.name, row.count) for row in rows[: min(top_n, 20)]]
    chart = hbar(chart_items, max_items=min(top_n, 20), value_formatter=lambda v: fmt_int(int(v)))
    table_rows = []
    for index, row in enumerate(rows[:top_n], start=1):
        table_rows.append(
            [
                str(index),
                _rule_cell(row),
                f'<span class="num">{fmt_int(row.count)}</span>',
                f'<span class="num">{fmt_pct(row.share_pct)}</span>',
                _delta_html(row.delta_pct),
                _z_html(row.z, config.baseline.z_threshold),
                f'<span class="num">{fmt_int(row.days_seen)}</span>',
                _severity_html(row.severity),
                _flags_html(row.flags),
            ]
        )
    body = chart + _table(
        ["#", "Правило", "Сработок", "Доля", "Δ к среднему", "z", "Дней", "Критичность", "Флаги"],
        table_rows,
        aligns=["left", "left", "right", "right", "right", "right", "right", "left", "left"],
        table_class="rules-table",
    )
    notes = []
    if data.rules_truncated:
        notes.append("Список правил усечён лимитом запроса — редкие правила могут отсутствовать.")
    if config.report.top_n < len(rows):
        notes.append(f"В таблице первые {config.report.top_n} правил; полный список — в rules.csv.")
    if notes:
        body += '<p class="note">' + " ".join(esc(note) for note in notes) + "</p>"
    return _card(
        "Топ правил",
        body,
        subtitle=f"Всего сработок: {fmt_int(data.total)} · правил: {fmt_int(len(data.rules))}",
    )


def _hourly_card(config: Config, data: DailyData) -> str:
    if not data.hourly:
        return _card(
            "Динамика по часам",
            '<p class="muted">Почасовая детализация отключена или недоступна.</p>',
            subtitle="Запросы по часовым срезам",
        )
    chart = line_chart(
        list(zip(data.hourly_labels, [float(x) for x in data.hourly])),
        value_formatter=lambda v: fmt_int(int(v)),
    )
    max_value = max(data.hourly) if data.hourly else 0
    peak = data.hourly.index(max_value) if data.hourly else 0
    peak_label = data.hourly_labels[peak] if peak < len(data.hourly_labels) else "—"
    night_total = 0
    for label, value in zip(data.hourly_labels, data.hourly):
        try:
            hour = int(str(label).split(":")[0])
        except (ValueError, IndexError):
            continue
        if hour < 6:
            night_total += value
    night_share = (night_total / data.total * 100.0) if data.total else 0.0
    stats = f"""
<div class="mini-stats">
  <div><span>Пиковый час</span><b>{esc(peak_label)}</b><span class="muted">{fmt_int(max_value)} сработок</span></div>
  <div><span>Ночью (00–06)</span><b>{esc(fmt_pct(night_share))}</b><span class="muted">от общего объёма</span></div>
  <div><span>Часовых срезов</span><b>{fmt_int(len(data.hourly))}</b><span class="muted">с шагом 1 час</span></div>
</div>
"""
    body = chart + stats

    if data.hourly_rules:
        top_rows = data.rules[:25]
        matrix: list[list[float]] = []
        labels: list[str] = []
        lookups = [
            {rule.name: rule.count for rule in bucket} for bucket in data.hourly_rules
        ]
        for rule in top_rows:
            labels.append(rule.name)
            matrix.append(
                [float(lookup.get(rule.name, 0)) for lookup in lookups]
            )
        if matrix:
            body += '<div class="heatmap-wrap">' + heatmap(
                matrix,
                labels,
                data.hourly_labels,
                value_formatter=lambda v: fmt_int(int(v)),
            ) + "</div>"
            body += (
                '<p class="note">Тепловая карта: топ-25 правил по часам. '
                "Наведите курсор на ячейку, чтобы увидеть точное значение.</p>"
            )
    if data.hourly_partial:
        body += (
            '<p class="note">Почасовая разбивка неполная: часть срезов ограничена лимитом '
            "или недоступна. Сумма по часам может быть меньше общего числа.</p>"
        )
    return _card(
        "Динамика по часам",
        body,
        subtitle="Распределение сработок внутри суток (локальное время)",
    )


def _severity_tactics(config: Config, data: DailyData) -> str:
    cards: list[str] = []
    if data.severity:
        items = [
            (severity_label(item.name), float(item.count), severity_color(item.name))
            for item in data.severity
        ]
        total = sum(item.count for item in data.severity)
        legend = []
        for item in data.severity:
            share = (item.count / total * 100.0) if total else 0.0
            legend.append(
                f'<div class="legend-row"><span class="legend-key">'
                f'<span class="legend-swatch" style="background:{esc(severity_color(item.name))}"></span>'
                f"{esc(severity_label(item.name))}</span>"
                f'<span>{fmt_int(item.count)} · {esc(fmt_pct(share))}</span></div>'
            )
        body = donut(items) + '<div class="legend">' + "".join(legend) + "</div>"
        cards.append(_card("Критичность", body, subtitle="Поле Severity коррелированных событий"))
    else:
        cards.append(
            _card(
                "Критичность",
                '<p class="muted">Поле Severity не заполнено или недоступно.</p>',
            )
        )

    if data.tactics:
        charts = hbar(
            [(item.name, item.count) for item in data.tactics[:10]],
            max_items=10,
            label_width=230,
            color=PALETTE[5],
            value_formatter=lambda v: fmt_int(int(v)),
        )
        cards.append(
            _card("MITRE ATT&CK: тактики", charts, subtitle="Поле Tactic коррелированных событий")
        )
    else:
        cards.append(
            _card(
                "MITRE ATT&CK",
                '<p class="muted">Тактики не заполнены или сбор отключён.</p>',
            )
        )

    if data.priority:
        charts = hbar(
            [(item.name, item.count) for item in data.priority[:10]],
            max_items=10,
            label_width=180,
            color=PALETTE[2],
            value_formatter=lambda v: fmt_int(int(v)),
        )
        cards.append(_card("Приоритеты", charts, subtitle="Поле Priority"))

    return '<div class="grid2">' + "".join(cards) + "</div>"


def _entities_card(config: Config, data: DailyData) -> str:
    cards: list[str] = []
    for field, values in data.entities.items():
        title = ENTITY_TITLES.get(field, field)
        if values:
            chart = hbar(
                [(item.name, item.count) for item in values[:10]],
                max_items=10,
                label_width=170,
                color=PALETTE[len(cards) % len(PALETTE)],
                value_formatter=lambda v: fmt_int(int(v)),
            )
            body = chart
            if data.raw.get("truncated"):
                body += '<p class="note">Уникальные значения — по выборке сырых событий.</p>'
            cards.append(_card(title, body, subtitle=f"Топ-10 значений поля {field}"))
        else:
            cards.append(
                _card(
                    title,
                    '<p class="muted">Нет данных по этому полю.</p>',
                    subtitle=f"Поле {field}",
                )
            )
    if not cards:
        return ""
    return _card(
        "Разрезы: источники, учётные записи, хосты",
        '<div class="grid2">' + "".join(cards) + "</div>",
        subtitle="Топ значений в сработках за период",
    )


def _tenants_card(data: DailyData) -> str:
    total = sum(item.count for item in data.tenants)
    rows = []
    for item in data.tenants:
        name = str(item.meta.get("name") or item.meta.get("tenant_id") or item.name)
        share = (item.count / total * 100.0) if total else 0.0
        rows.append(
            [
                esc(name),
                f'<span class="num">{fmt_int(item.count)}</span>',
                f'<span class="num">{fmt_pct(share)}</span>',
            ]
        )
    return _card(
        "Тенанты",
        _table(["Тенант", "Сработок", "Доля"], rows, aligns=["left", "right", "right"]),
        subtitle="Распределение по TenantID",
    )


def _silent_card(
    config: Config, data: DailyData, baseline: Baseline, silent: list[RuleRow]
) -> str:
    subtitle = f"Правила со средней активностью ≥ {fmt_int(config.baseline.min_events)} в сутки"
    if data.rules_truncated:
        body = (
            '<p class="muted">Анализ молчащих правил пропущен: список сработок усечён '
            "лимитом, нельзя надёжно определить отсутствие правила.</p>"
        )
    elif not baseline.enough_data:
        body = (
            '<p class="muted">Нужна история не менее '
            f"{config.baseline.min_days} дней. Сейчас доступно {baseline.days_available}.</p>"
        )
    elif not silent:
        body = '<p class="muted">Все ранее активные правила срабатывали в этот день.</p>'
    else:
        rows = []
        for row in silent[:30]:
            rows.append(
                [
                    _rule_cell(row),
                    f'<span class="num">{fmt_int(row.mean)}</span>',
                    f'<span class="num">{fmt_int(row.max_seen)}</span>',
                    f'<span class="num">{fmt_int(row.days_seen)}</span>',
                    esc(_local_date(row.last_seen_day) if row.last_seen_day else "—"),
                ]
            )
        body = _table(
            ["Правило", "Среднее", "Макс.", "Дней", "Последняя сработка"],
            rows,
            aligns=["left", "right", "right", "right", "left"],
        )
        if len(silent) > 30:
            body += f'<p class="note">Показаны первые 30 из {len(silent)}. Полный список — в CSV.</p>'
    return _card(
        "Правила без сработок (по истории)",
        body,
        subtitle=subtitle,
    )


def _inventory_card(
    config: Config,
    data: DailyData,
    baseline: Baseline,
    inventory_info: dict[str, list[NamedCount]],
) -> str:
    total = len(data.inventory)
    not_today = inventory_info.get("not_today") or []
    long_silent = inventory_info.get("long_silent") or []
    cards = (
        '<div class="mini-stats">'
        f"<div><span>Правил в инвентаре</span><b>{fmt_int(total)}</b><span class=\"muted\">ресурсы KUMA</span></div>"
        f"<div><span>Не срабатывали сегодня</span><b>{fmt_int(len(not_today))}</b><span class=\"muted\">по названию</span></div>"
        f"<div><span>Молчат &gt; {config.baseline.new_rule_days} дн.</span><b>{fmt_int(len(long_silent))}</b><span class=\"muted\">нет в истории</span></div>"
        "</div>"
    )
    rows = []
    for item in long_silent[:30]:
        rows.append(
            [
                esc(item.name),
                esc(item.meta.get("id") or "—"),
                esc(item.meta.get("tenant") or "—"),
            ]
        )
    body = cards
    if rows:
        body += _table(
            ["Правило", "ID ресурса", "Тенант"],
            rows,
            aligns=["left", "left", "left"],
        )
        if len(long_silent) > 30:
            body += f'<p class="note">Показаны первые 30 из {len(long_silent)}.</p>'
    if baseline.days_available == 0:
        body += (
            '<p class="note">Истории ещё нет: колонка «молчат» станет информативной '
            "после нескольких запусков.</p>"
        )
    body += (
        '<p class="note">Список ресурсов KUMA не содержит признака «включено/выключено», '
        "поэтому молчащие правила требуют ручной проверки.</p>"
    )
    return _card("Инвентарь правил корреляции", body, subtitle="Сопоставление ресурсов и фактических сработок")


def _history_card(config: Config, baseline: Baseline) -> str:
    if not baseline.days:
        return _card(
            "История",
            '<p class="muted">История пуста: это первый запуск по кластеру. '
            "График появится после накопления данных.</p>",
        )
    series = baseline.days[-14:]
    items = [
        (_short_day(day), float(baseline.totals.get(day, 0))) for day in series
    ]
    chart = line_chart(
        items,
        mean_line=baseline.mean,
        value_formatter=lambda v: fmt_int(int(v)),
        x_label_step=max(1, len(items) // 7) if items else None,
        empty_text="История пуста",
    )
    rows = []
    previous: int | None = None
    for day, value in list(baseline.totals.items())[-7:]:
        delta = None if previous is None else (value - previous) / previous * 100.0 if previous else None
        deviation = (
            (value - baseline.mean) / baseline.mean * 100.0
            if baseline.mean
            else None
        )
        rows.append(
            [
                esc(_local_date(day)),
                f'<span class="num">{fmt_int(value)}</span>',
                _delta_html(delta),
                _delta_html(deviation),
            ]
        )
        previous = value
    body = chart + _table(
        ["Дата", "Сработок", "Δ к пред. дню", "Δ к среднему"],
        rows,
        aligns=["left", "right", "right", "right"],
    )
    note = _history_note(config, baseline)
    if note:
        body += f'<p class="note">{esc(note)}</p>'
    return _card(
        "История",
        body,
        subtitle=f"Последние {len(series)} дней · среднее {fmt_int(baseline.mean)} сработок/сутки",
    )


def _appendix(config: Config, data: DailyData, rows: list[RuleRow]) -> str:
    files = [
        ("summary.json", "Сводка (JSON)"),
        ("snapshot.json", "Снимок данных"),
        ("rules.csv", "Правила"),
        ("anomalies.csv", "Аномалии"),
        ("silent_rules.csv", "Молчащие правила"),
        ("hourly.csv", "Почасовая динамика"),
        ("hourly_rules.csv", "Правила по часам"),
        ("queries.csv", "Журнал запросов"),
    ]
    for field in data.entities:
        files.append((f"entity_{field}.csv", f"Разрез {field}"))
    if data.tactics:
        files.append(("tactics.csv", "Тактики MITRE"))
    if data.priority:
        files.append(("priority.csv", "Приоритеты"))
    if data.tenants:
        files.append(("tenants.csv", "Тенанты"))
    if data.inventory:
        files.append(("inventory_silent.csv", "Молчащие правила (инвентарь)"))

    file_links = "".join(
        f'<a class="file-link" href="{esc(quote(name))}">{esc(label)}</a>'
        for name, label in files
    )

    query_rows = []
    for index, stat in enumerate(data.query_stats, start=1):
        status = (
            '<span class="status-ok">OK</span>'
            if stat.ok
            else f'<span class="status-fail" title="{esc(stat.error)}">Ошибка</span>'
        )
        query_rows.append(
            [
                str(index),
                esc(stat.name),
                f'<span class="num">{fmt_int(stat.rows)}</span>',
                f'<span class="num">{fmt_int(stat.duration_ms)}</span>',
                status,
                f'<code class="pre">{esc(truncate(stat.sql, 220))}</code>',
            ]
        )
    query_table = _table(
        ["#", "Запрос", "Строк", "мс", "Статус", "SQL"],
        query_rows,
        aligns=["left", "left", "right", "right", "left", "left"],
    )

    method = f"""
<p class="small muted">
  Период: {esc(_local_time(data.period_from, load_timezone(config.report.timezone, config.report.utc_offset_hours)[0]))}
  — {esc(_local_time(data.period_to, load_timezone(config.report.timezone, config.report.utc_offset_hours)[0]))}
  (границы KUMA включительные, конец сдвинут на 1 мс).
  События: Type={data.event_type} (коррелированные). Запросов: {len(data.query_stats)},
  длительность сбора: {esc(fmt_duration(data.duration_sec))}.
  Аномалия — отклонение от среднего за историю более чем на {esc(fmt_float(config.baseline.z_threshold))} σ
  при минимуме {fmt_int(config.baseline.min_events)} сработок и абсолютном приросте
  не менее {fmt_int(config.baseline.min_delta)}. «Шум» — стабильно высокий объём без значимых отклонений.
</p>
"""
    body = (
        f'<div class="files">{file_links}</div>'
        f"<h3 style=\"font-size:14px;margin:18px 0 6px\">Журнал запросов</h3>{query_table}"
        f"<h3 style=\"font-size:14px;margin:18px 0 6px\">Методика</h3>{method}"
    )
    return _card(
        "Приложение: данные, запросы и файлы",
        body,
        subtitle=f"Инструмент {__version__} · правил в отчёте: {fmt_int(len(rows))}",
    )


# --------------------------------------------------------------------------- #
# Мелкие HTML-хелперы
# --------------------------------------------------------------------------- #

def _card(
    title: str,
    body: str,
    subtitle: str = "",
    anchor: str = "",
    extra_class: str = "",
) -> str:
    anchor_attr = f' id="{esc(anchor)}"' if anchor else ""
    sub = f'<div class="card-sub">{subtitle}</div>' if subtitle else ""
    return f"""
<section class="card {extra_class}"{anchor_attr}>
  <div class="card-head"><div><h2>{esc(title)}</h2>{sub}</div></div>
  {body}
</section>
"""


def _table(
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    aligns: Sequence[str] | None = None,
    table_class: str = "",
) -> str:
    aligns = list(aligns or ["left"] * len(headers))
    head = "".join(
        f'<th class="{esc(aligns[i] if i < len(aligns) else "left")}">{esc(header)}</th>'
        for i, header in enumerate(headers)
    )
    body_rows = []
    for row in rows:
        cells = "".join(
            f'<td class="{esc(aligns[i] if i < len(aligns) else "left")}">{cell}</td>'
            for i, cell in enumerate(row)
        )
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        f'<div class="table-wrap"><table class="{esc(table_class)}">'
        f"<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def _rule_cell(row: RuleRow) -> str:
    rule_id = f'<span class="rule-id">{esc(row.rule_id)}</span>' if row.rule_id else ""
    return f'<div class="rule-name">{esc(row.name)}{rule_id}</div>'


def _severity_html(value: str) -> str:
    if not value:
        return '<span class="muted">—</span>'
    return (
        f'<span class="sev"><span class="dot" style="background:{esc(severity_color(value))}"></span>'
        f"{esc(severity_label(value))}</span>"
    )


def _flags_html(flags: Sequence[str]) -> str:
    if not flags:
        return '<span class="muted">—</span>'
    return "".join(
        f'<span class="badge {esc(FLAG_CLASSES.get(flag, "flag-silent"))}">{esc(flag)}</span>'
        for flag in flags
    )


def _delta_html(value: float | None) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    cls = "up" if value > 0 else "down" if value < 0 else ""
    class_attr = f' class="{cls}"' if cls else ""
    return f"<span{class_attr}>{esc(fmt_pct(value, signed=True))}</span>"


def _z_html(value: float | None, threshold: float) -> str:
    if value is None:
        return '<span class="muted">—</span>'
    cls = "up" if value >= threshold else "down" if value <= -threshold else ""
    class_attr = f' class="{cls}"' if cls else ""
    return f"<span{class_attr}>{esc(fmt_float(value))}</span>"


def _history_note(config: Config, baseline: Baseline) -> str:
    if baseline.days_available == 0:
        return (
            "Истории ещё нет: это первый отчёт. Сравнение и аномалии появятся "
            "после нескольких ежедневных запусков."
        )
    if not baseline.enough_data:
        return (
            f"Истории недостаточно для аномалий: {baseline.days_available} из "
            f"{config.baseline.min_days} требуемых дней."
        )
    return ""


def rows_top_share(data: DailyData) -> float:
    if not data.rules or not data.total:
        return 0.0
    return data.rules[0].count / data.total * 100.0


def _local_time(value: str, tz, seconds: bool = False) -> str:
    parsed = parse_dt(value)
    if not parsed:
        return value or "—"
    fmt = "%d.%m.%Y %H:%M:%S" if seconds else "%d.%m.%Y %H:%M"
    return parsed.astimezone(tz).strftime(fmt)


def _local_date(value: str) -> str:
    if not value:
        return "—"
    try:
        return parse_dt(value).strftime("%d.%m.%Y")
    except (AttributeError, ValueError):
        return value


def _short_day(value: str) -> str:
    try:
        from datetime import date

        return date.fromisoformat(value).strftime("%d.%m")
    except ValueError:
        return value


# --------------------------------------------------------------------------- #
# CSV / JSON
# --------------------------------------------------------------------------- #

def _write_csvs(
    day_dir: Path,
    data: DailyData,
    rows: list[RuleRow],
    anomalies: list[RuleRow],
    silent: list[RuleRow],
    inventory_info: dict[str, list[NamedCount]] | None,
) -> list[Path]:
    files: list[Path] = []

    def add(name: str, headers: list[str], body: list[list[Any]]) -> None:
        path = day_dir / name
        write_csv(path, headers, body)
        files.append(path)

    rule_headers = [
        "Место",
        "Правило",
        "CorrelationRuleID",
        "Сработок",
        "Доля, %",
        "Среднее за историю",
        "Среднее 7 дней",
        "Максимум",
        "Медиана",
        "Δ к среднему, %",
        "z-score",
        "Дней сработок",
        "Критичность",
        "Уникальных источников",
        "Первая сработка",
        "Последняя сработка",
        "Флаги",
    ]

    def rule_row(index: int, row: RuleRow) -> list[Any]:
        return [
            index,
            row.name,
            row.rule_id,
            row.count,
            f"{row.share_pct:.2f}",
            _num(row.mean),
            _num(row.mean7),
            row.max_seen,
            _num(row.median),
            _num(row.delta_pct),
            _num(row.z),
            row.days_seen,
            row.severity,
            row.unique_sources,
            row.first_seen,
            row.last_seen,
            ", ".join(row.flags),
        ]

    add("rules.csv", rule_headers, [rule_row(i, row) for i, row in enumerate(rows, 1)])
    add(
        "anomalies.csv",
        rule_headers,
        [rule_row(i, row) for i, row in enumerate(anomalies, 1)],
    )
    add(
        "silent_rules.csv",
        ["Правило", "Среднее", "Среднее 7 дней", "Максимум", "Медиана", "Дней сработок", "Последняя сработка"],
        [
            [
                row.name,
                _num(row.mean),
                _num(row.mean7),
                row.max_seen,
                _num(row.median),
                row.days_seen,
                row.last_seen_day,
            ]
            for row in silent
        ],
    )

    if data.hourly:
        add(
            "hourly.csv",
            ["Час", "Сработок"],
            [[label, value] for label, value in zip(data.hourly_labels, data.hourly)],
        )
    if data.hourly_rules:
        hourly_rows = []
        for label, bucket in zip(data.hourly_labels, data.hourly_rules):
            for rule in bucket:
                hourly_rows.append([label, rule.name, rule.count])
        add("hourly_rules.csv", ["Час", "Правило", "Сработок"], hourly_rows)

    for field, values in data.entities.items():
        add(
            f"entity_{field}.csv",
            [field, "Сработок"],
            [[item.name, item.count] for item in values],
        )
    if data.tactics:
        add("tactics.csv", ["Тактика", "Сработок"], [[x.name, x.count] for x in data.tactics])
    if data.priority:
        add("priority.csv", ["Приоритет", "Сработок"], [[x.name, x.count] for x in data.priority])
    if data.tenants:
        add(
            "tenants.csv",
            ["TenantID", "Тенант", "Сработок"],
            [[x.name, x.meta.get("name", ""), x.count] for x in data.tenants],
        )
    if inventory_info is not None:
        inv_rows = []
        for section, key in (
            ("не срабатывало сегодня", "not_today"),
            ("молчит продолжительное время", "long_silent"),
        ):
            for item in inventory_info.get(key, []):
                inv_rows.append(
                    [section, item.name, item.meta.get("id", ""), item.meta.get("tenant", "")]
                )
        add(
            "inventory_silent.csv",
            ["Раздел", "Правило", "ID ресурса", "Тенант"],
            inv_rows,
        )
    add(
        "queries.csv",
        ["Запрос", "SQL", "Строк", "мс", "OK", "Ошибка"],
        [
            [q.name, q.sql, q.rows, q.duration_ms, "да" if q.ok else "нет", q.error]
            for q in data.query_stats
        ],
    )
    return files


def _num(value: float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        return f"{value:.2f}"
    return str(value)


def _build_summary(
    config: Config,
    data: DailyData,
    baseline: Baseline,
    rows: list[RuleRow],
    anomalies: list[RuleRow],
    silent: list[RuleRow],
) -> dict[str, Any]:
    raw = data.raw if isinstance(data.raw, dict) else {}
    unique = raw.get("unique") if isinstance(raw.get("unique"), dict) else {}
    return {
        "day": data.day,
        "generated_at": data.generated_at,
        "period_from": data.period_from,
        "period_to": data.period_to,
        "timezone": data.timezone,
        "cluster_id": data.cluster_id,
        "cluster_name": data.cluster_name,
        "event_type": data.event_type,
        "total": data.total,
        "unique_rules": len(data.rules),
        "unique_sources": unique.get("SourceAddress"),
        "base_events": data.base_events,
        "anomalies": len(anomalies),
        "new_rules": sum(1 for row in anomalies if "новое" in row.flags),
        "silent_rules": len(silent),
        "total_delta_pct": baseline.total_delta_pct,
        "total_z": baseline.total_z,
        "history_days": baseline.days_available,
        "warning_count": len(data.warnings),
        "top_rules": [
            {
                "name": row.name,
                "count": row.count,
                "share_pct": row.share_pct,
                "severity": row.severity,
            }
            for row in rows[:10]
        ],
        "top_anomalies": [
            {
                "name": row.name,
                "count": row.count,
                "flags": row.flags,
                "delta_pct": row.delta_pct,
                "z": row.z,
            }
            for row in anomalies[:10]
        ],
        "report": "report.html",
        "tool_version": __version__,
    }


def _write_index(config: Config) -> None:
    output = Path(config.report.output_dir)
    ensure_dir(output)
    entries: list[tuple[str, dict[str, Any]]] = []
    for child in output.iterdir():
        if not child.is_dir():
            continue
        summary_path = child / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = read_json(summary_path)
        except (OSError, ValueError):
            continue
        if isinstance(summary, dict) and summary.get("day"):
            entries.append((str(summary["day"]), summary))
    entries.sort(key=lambda item: item[0], reverse=True)
    entries = entries[: config.report.index_limit]

    rows = []
    for day, summary in entries:
        link = f"{quote(day)}/report.html"
        rows.append(
            [
                f'<a href="{esc(link)}">{esc(_local_date(day))}</a>',
                f'<span class="num">{fmt_int(summary.get("total"))}</span>',
                f'<span class="num">{fmt_int(summary.get("unique_rules"))}</span>',
                f'<span class="num">{fmt_int(summary.get("anomalies"))}</span>',
                f'<span class="num">{fmt_int(summary.get("new_rules"))}</span>',
                f'<span class="num">{fmt_int(summary.get("warning_count"))}</span>',
                f'<a href="{esc(link)}">Открыть</a>',
            ]
        )
    table = (
        _table(
            ["Дата", "Сработок", "Правил", "Аномалий", "Новых", "Предупр.", "Отчёт"],
            rows,
            aligns=["left", "right", "right", "right", "right", "right", "left"],
            table_class="index-table",
        )
        if rows
        else '<p class="muted">Отчётов пока нет.</p>'
    )
    body = f"""
<div class="page">
<header class="hero">
  <div class="eyebrow">KUMA · архив</div>
  <h1>{esc(config.report.title)}</h1>
  <div class="sub">Ежедневные отчёты по сработкам правил корреляции. Всего сохранено: {len(entries)}.</div>
</header>
{_card("Отчёты", table, subtitle="Новые сверху")}
<div class="footer">Обновлено автоматически · kuma-daily-stats {esc(__version__)}</div>
</div>
"""
    (output / "index.html").write_text(_page("Архив отчётов KUMA", body), encoding="utf-8")


# --------------------------------------------------------------------------- #
# CSS
# --------------------------------------------------------------------------- #

_CSS = """
:root{
  --bg:#f4f6fb;--card:#ffffff;--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;
  --accent:#2563eb;--accent-soft:#eff6ff;--up:#b91c1c;--down:#047857;
  --warn:#b45309;--warn-bg:#fffbeb;--info:#1d4ed8;--info-bg:#eff6ff;--radius:16px;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.page{max-width:1220px;margin:0 auto;padding:28px 22px 64px}
.hero{position:relative;background:radial-gradient(circle at 15% 0%,#1e40af 0,#0f172a 58%);
  color:#fff;border-radius:22px;padding:30px 34px;overflow:hidden;
  box-shadow:0 22px 45px -24px rgba(15,23,42,.7)}
.hero::after{content:"";position:absolute;right:-80px;bottom:-150px;width:340px;height:340px;
  border-radius:50%;background:rgba(59,130,246,.22)}
.eyebrow{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:#bfdbfe;font-weight:600}
.hero h1{margin:8px 0 10px;font-size:30px;line-height:1.2;letter-spacing:-.01em}
.hero .sub{color:#dbeafe;font-size:14px;max-width:840px}
.meta{display:flex;flex-wrap:wrap;gap:8px 18px;margin-top:16px;font-size:12.5px;color:#dbeafe}
.meta b{color:#fff;font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(176px,1fr));gap:14px;margin:20px 0 6px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:16px 18px;box-shadow:0 10px 25px -22px rgba(15,23,42,.5)}
.kpi .label{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600}
.kpi .value{font-size:27px;font-weight:750;letter-spacing:-.02em;margin-top:6px}
.kpi .hint{font-size:12px;color:var(--muted);margin-top:4px}
.kpi.warn .value{color:var(--up)}
.kpi.good .value{color:var(--down)}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  padding:22px 24px;margin:18px 0;box-shadow:0 14px 30px -26px rgba(15,23,42,.45)}
.card h2{margin:0;font-size:19px;letter-spacing:-.01em}
.card-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;
  margin-bottom:12px;flex-wrap:wrap}
.card-sub{color:var(--muted);font-size:12.5px;margin-top:4px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(430px,1fr));gap:18px}
.grid2>.card{margin:0}
@media(max-width:640px){.grid2{grid-template-columns:1fr}}
.table-wrap{overflow-x:auto;margin:6px -4px 0;padding:0 4px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);
  font-weight:650;text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #f1f5f9;vertical-align:top}
tbody tr:hover{background:#f8fafc}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.center{text-align:center}
.rule-name{max-width:430px;word-break:break-word}
.rule-id{display:block;color:#94a3b8;font-size:11px;margin-top:2px}
.up{color:var(--up);font-weight:600}
.down{color:var(--down);font-weight:600}
.muted{color:var(--muted)}
.small{font-size:12px}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;
  white-space:nowrap;margin:1px 3px 1px 0;border:1px solid transparent}
.flag-new{background:#eef2ff;color:#4338ca;border-color:#c7d2fe}
.flag-up{background:#fef2f2;color:#b91c1c;border-color:#fecaca}
.flag-down{background:#ecfdf5;color:#047857;border-color:#a7f3d0}
.flag-noise{background:#fffbeb;color:#b45309;border-color:#fde68a}
.flag-silent{background:#f1f5f9;color:#475569;border-color:#cbd5e1}
.sev{display:inline-flex;align-items:center;gap:6px;font-size:12px;white-space:nowrap}
.sev .dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.alerts{list-style:none;margin:0;padding:0;display:grid;gap:9px}
.alerts li{padding:11px 14px;border-radius:12px;border:1px solid #fde68a;
  background:var(--warn-bg);color:#7c2d12;font-size:13.5px}
.alerts li.info{border-color:#bfdbfe;background:var(--info-bg);color:#1e3a8a}
.alerts li.danger{border-color:#fecaca;background:#fef2f2;color:#7f1d1d}
.note{font-size:12.5px;color:var(--muted);margin-top:10px}
.chart-svg{width:100%;height:auto;display:block;margin-top:4px}
svg text{font-family:inherit}
.heatmap-wrap{overflow-x:auto;margin-top:10px}
.legend{display:grid;gap:7px;margin-top:14px}
.legend-row{display:flex;align-items:center;justify-content:space-between;gap:12px;
  font-size:13px;border-bottom:1px dashed #eef2f7;padding-bottom:6px}
.legend-key{display:flex;align-items:center;gap:8px}
.legend-swatch{width:10px;height:10px;border-radius:3px;display:inline-block}
.mini-stats{display:flex;flex-wrap:wrap;gap:26px;margin:14px 0 4px}
.mini-stats>div{display:flex;flex-direction:column;gap:2px;min-width:130px}
.mini-stats span{font-size:11.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.mini-stats b{font-size:20px;line-height:1.2}
.mini-stats .muted{text-transform:none;letter-spacing:0;font-size:12px}
.files{display:flex;flex-wrap:wrap;gap:8px}
.file-link{display:inline-block;padding:7px 12px;border-radius:10px;background:#f8fafc;
  border:1px solid var(--line);font-size:12.5px}
code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;
  background:#f8fafc;border:1px solid #eef2f7;border-radius:6px;padding:1px 5px;word-break:break-all}
.pre{white-space:pre-wrap;word-break:break-word}
.footer{margin-top:36px;color:var(--muted);font-size:12px;text-align:center;line-height:1.7}
.status-ok{color:#047857;font-weight:600}
.status-fail{color:#b91c1c;font-weight:600}
.index-table td{vertical-align:middle}
@media print{
  body{background:#fff}
  .page{max-width:none;padding:0}
  .hero{box-shadow:none;border-radius:0}
  .card{box-shadow:none;break-inside:avoid;page-break-inside:avoid}
  .kpis{break-inside:avoid}
  a{color:inherit;text-decoration:none}
}
"""
