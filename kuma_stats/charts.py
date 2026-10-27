"""Лёгкие SVG-графики без внешних зависимостей."""
from __future__ import annotations

import math
from typing import Callable, Sequence

from .utils import esc, fmt_int, truncate

PALETTE = [
    "#2563eb",
    "#0ea5e9",
    "#14b8a6",
    "#f59e0b",
    "#ef4444",
    "#8b5cf6",
    "#10b981",
    "#f97316",
    "#6366f1",
    "#64748b",
]


def hbar(
    items: Sequence[tuple[str, float]],
    *,
    width: int = 920,
    max_items: int = 20,
    color: str = "#2563eb",
    colors: Sequence[str] | None = None,
    label_width: int = 280,
    value_formatter: Callable[[float], str] = fmt_int,
    empty_text: str = "Нет данных за период",
) -> str:
    data = [(str(label), float(value)) for label, value in items][: max(1, max_items)]
    if not data:
        return _empty_state(empty_text, width)

    row_height = 30
    bar_height = 16
    top = 18
    bottom = 34
    right = 84
    height = top + row_height * len(data) + bottom
    plot_width = max(60, width - label_width - right)
    top_value = _nice_max(max(value for _, value in data))
    ticks = _ticks(top_value)
    bar_area_left = label_width

    parts: list[str] = []
    for tick in ticks:
        x = bar_area_left + (tick / top_value) * plot_width
        parts.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{height - bottom}" '
            'stroke="#e2e8f0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{x:.1f}" y="{height - bottom + 16}" text-anchor="middle" '
            f'font-size="10" fill="#64748b">{esc(_tick_label(tick))}</text>'
        )

    for index, (label, value) in enumerate(data):
        y = top + index * row_height
        bar_width = max(2.0, (value / top_value) * plot_width) if top_value else 2.0
        fill = colors[index] if colors and index < len(colors) else color
        full_label = label or "—"
        parts.append(
            f'<text x="{bar_area_left - 10}" y="{y + bar_height / 2 + 4}" '
            f'text-anchor="end" font-size="12" fill="#0f172a">'
            f'<title>{esc(full_label)}</title>{esc(truncate(full_label, 44))}</text>'
        )
        parts.append(
            f'<rect x="{bar_area_left}" y="{y}" width="{bar_width:.1f}" height="{bar_height}" '
            f'rx="4" fill="{fill}" opacity="{0.35 if value == 0 else 0.9}">'
            f'<title>{esc(full_label)}: {esc(value_formatter(value))}</title></rect>'
        )
        parts.append(
            f'<text x="{bar_area_left + bar_width + 8:.1f}" y="{y + bar_height / 2 + 4}" '
            f'font-size="12" fill="#334155">{esc(value_formatter(value))}</text>'
        )

    return _wrap(width, height, "".join(parts), "Горизонтальная диаграмма")


def line_chart(
    items: Sequence[tuple[str, float]],
    *,
    width: int = 920,
    height: int = 280,
    color: str = "#2563eb",
    fill_area: bool = True,
    mean_line: float | None = None,
    value_formatter: Callable[[float], str] = fmt_int,
    x_label_step: int | None = None,
    empty_text: str = "Нет данных за период",
) -> str:
    points = [(str(label), float(value)) for label, value in items]
    if not points:
        return _empty_state(empty_text, width, 150)

    left = 60
    right = 18
    top = 18
    bottom = 42
    plot_width = max(60, width - left - right)
    plot_height = max(60, height - top - bottom)
    top_value = _nice_max(max(value for _, value in points))
    if mean_line is not None:
        top_value = max(top_value, _nice_max(mean_line))

    count = len(points)
    step_x = plot_width / (count - 1) if count > 1 else 0.0
    xs = [left + (index * step_x if count > 1 else plot_width / 2) for index in range(count)]

    parts: list[str] = []
    for tick in _ticks(top_value):
        y = top + plot_height - (tick / top_value) * plot_height
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            'stroke="#e2e8f0" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
            f'fill="#64748b">{esc(_tick_label(tick))}</text>'
        )

    ys = [top + plot_height - (value / top_value) * plot_height for _, value in points]
    if fill_area and count > 1:
        area = (
            f"M{xs[0]:.1f},{top + plot_height:.1f} "
            + " ".join(f"L{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
            + f" L{xs[-1]:.1f},{top + plot_height:.1f} Z"
        )
        parts.append(f'<path d="{area}" fill="{color}" opacity="0.10"/>')

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    parts.append(
        f'<polyline points="{polyline}" fill="none" stroke="{color}" stroke-width="2.4" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )

    if mean_line is not None and mean_line > 0:
        mean_y = top + plot_height - (mean_line / top_value) * plot_height
        parts.append(
            f'<line x1="{left}" y1="{mean_y:.1f}" x2="{width - right}" y2="{mean_y:.1f}" '
            'stroke="#94a3b8" stroke-width="1.2" stroke-dasharray="5 4"/>'
        )
        parts.append(
            f'<text x="{width - right}" y="{mean_y - 6:.1f}" text-anchor="end" font-size="10" '
            f'fill="#64748b">среднее {esc(value_formatter(mean_line))}</text>'
        )

    for (label, value), x, y in zip(points, xs, ys):
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="#ffffff" stroke="{color}" '
            f'stroke-width="2"><title>{esc(label)}: {esc(value_formatter(value))}</title></circle>'
        )

    label_step = x_label_step or max(1, math.ceil(count / 12))
    for index, (label, _) in enumerate(points):
        if index % label_step != 0 and index != count - 1:
            continue
        parts.append(
            f'<text x="{xs[index]:.1f}" y="{height - 14}" text-anchor="middle" font-size="10" '
            f'fill="#64748b">{esc(label)}</text>'
        )

    return _wrap(width, height, "".join(parts), "Линейный график")


def donut(
    items: Sequence[tuple[str, float, str]],
    *,
    size: int = 240,
    thickness: int = 40,
    empty_text: str = "Нет данных",
) -> str:
    data = [(str(label), float(value), color) for label, value, color in items if float(value) > 0]
    total = sum(value for _, value, _ in data)
    if not data or total <= 0:
        return _empty_state(empty_text, size, size)

    center = size / 2.0
    outer = size / 2.0 - 6
    inner = outer - thickness
    parts: list[str] = []

    if len(data) == 1:
        label, value, color = data[0]
        parts.append(
            f'<circle cx="{center}" cy="{center}" r="{(outer + inner) / 2:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="{thickness}">'
            f'<title>{esc(label)}: {esc(fmt_int(value))} (100 %)</title></circle>'
        )
    else:
        angle = -90.0
        for label, value, color in data:
            sweep = value / total * 360.0
            gap = min(0.8, sweep * 0.15)
            parts.append(
                _arc_path(
                    center,
                    center,
                    outer,
                    inner,
                    angle + gap / 2,
                    angle + sweep - gap / 2,
                    color,
                    f"{label}: {fmt_int(value)} ({value / total * 100:.1f} %)".replace(".", ","),
                )
            )
            angle += sweep

    parts.append(
        f'<text x="{center}" y="{center - 2}" text-anchor="middle" font-size="26" '
        f'font-weight="700" fill="#0f172a">{esc(fmt_int(total))}</text>'
    )
    parts.append(
        f'<text x="{center}" y="{center + 18}" text-anchor="middle" font-size="11" '
        'fill="#64748b">сработок</text>'
    )
    return _wrap(size, size, "".join(parts), "Кольцевая диаграмма")


def heatmap(
    matrix: Sequence[Sequence[float]],
    row_labels: Sequence[str],
    col_labels: Sequence[str],
    *,
    cell_width: int = 34,
    cell_height: int = 22,
    row_label_width: int = 300,
    color: str = "#2563eb",
    value_formatter: Callable[[float], str] = fmt_int,
    empty_text: str = "Нет данных для тепловой карты",
) -> str:
    rows = len(matrix)
    if rows == 0 or not row_labels or not col_labels:
        return _empty_state(empty_text, 900, 150)

    cols = len(col_labels)
    top = 22
    bottom = 10
    width = row_label_width + cols * cell_width + 16
    height = top + rows * cell_height + bottom
    max_value = max((float(value) for row in matrix for value in row), default=0.0)

    parts: list[str] = []
    for column, label in enumerate(col_labels):
        if column % 2 == 0:
            x = row_label_width + column * cell_width + cell_width / 2
            parts.append(
                f'<text x="{x:.1f}" y="14" text-anchor="middle" font-size="9" '
                f'fill="#64748b">{esc(label)}</text>'
            )

    for row_index, row in enumerate(matrix):
        y = top + row_index * cell_height
        label = row_labels[row_index] if row_index < len(row_labels) else ""
        parts.append(
            f'<text x="{row_label_width - 8}" y="{y + cell_height / 2 + 4}" text-anchor="end" '
            f'font-size="11" fill="#0f172a"><title>{esc(label)}</title>'
            f'{esc(truncate(label, 46))}</text>'
        )
        for column in range(cols):
            value = float(row[column]) if column < len(row) else 0.0
            x = row_label_width + column * cell_width
            if value > 0 and max_value > 0:
                opacity = 0.12 + 0.88 * math.sqrt(value / max_value)
            else:
                opacity = 0.0
            background = color if value > 0 else "#f1f5f9"
            parts.append(
                f'<rect x="{x + 1}" y="{y + 1}" width="{cell_width - 2}" height="{cell_height - 2}" '
                f'rx="3" fill="{background}" opacity="{opacity if value > 0 else 0.55}">'
                f'<title>{esc(label)} · {esc(col_labels[column])}: {esc(value_formatter(value))}</title>'
                "</rect>"
            )
            if value > 0 and cell_width >= 30:
                text_color = "#ffffff" if opacity > 0.55 else "#1e3a8a"
                parts.append(
                    f'<text x="{x + cell_width / 2:.1f}" y="{y + cell_height / 2 + 3.5}" '
                    f'text-anchor="middle" font-size="8.5" fill="{text_color}">'
                    f"{esc(value_formatter(value))}</text>"
                )

    return _wrap(width, height, "".join(parts), "Тепловая карта правил по часам")


# --------------------------------------------------------------------------- #
# Служебные
# --------------------------------------------------------------------------- #

def _wrap(width: int, height: int, body: str, title: str) -> str:
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg" role="img" '
        f'aria-label="{esc(title)}" preserveAspectRatio="xMidYMid meet">'
        f"<title>{esc(title)}</title>{body}</svg>"
    )


def _empty_state(text: str, width: int = 920, height: int = 120) -> str:
    body = (
        f'<rect x="0" y="0" width="{width}" height="{height}" rx="8" fill="#f8fafc"/>'
        f'<text x="{width / 2}" y="{height / 2 + 4}" text-anchor="middle" font-size="13" '
        f'fill="#64748b">{esc(text)}</text>'
    )
    return _wrap(width, height, body, text)


def _nice_max(value: float) -> float:
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        if fraction <= step:
            return step * (10 ** exponent)
    return 10.0 ** (exponent + 1)


def _ticks(top_value: float, count: int = 4) -> list[float]:
    if top_value <= 0:
        return [0.0]
    if top_value <= count:
        step = 1
    else:
        raw_step = top_value / count
        exponent = math.floor(math.log10(raw_step))
        fraction = raw_step / (10 ** exponent)
        for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
            if fraction <= candidate:
                step = candidate * (10 ** exponent)
                break
        else:
            step = 10 ** (exponent + 1)
    ticks: list[float] = []
    value = 0.0
    while value <= top_value + 1e-9:
        ticks.append(round(value, 6))
        value += step
    if ticks and abs(ticks[-1] - top_value) > 1e-6:
        ticks.append(round(top_value, 6))
    return ticks


def _tick_label(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return fmt_int(int(round(value)))
    return f"{value:.1f}".replace(".", ",")


def _arc_path(
    cx: float,
    cy: float,
    outer: float,
    inner: float,
    start_angle: float,
    end_angle: float,
    color: str,
    tooltip: str,
) -> str:
    if end_angle <= start_angle:
        return ""
    large_arc = 1 if (end_angle - start_angle) > 180 else 0
    start_outer = _polar(cx, cy, outer, start_angle)
    end_outer = _polar(cx, cy, outer, end_angle)
    start_inner = _polar(cx, cy, inner, start_angle)
    end_inner = _polar(cx, cy, inner, end_angle)
    path = (
        f"M{start_outer[0]:.2f},{start_outer[1]:.2f} "
        f"A{outer:.2f},{outer:.2f} 0 {large_arc} 1 {end_outer[0]:.2f},{end_outer[1]:.2f} "
        f"L{end_inner[0]:.2f},{end_inner[1]:.2f} "
        f"A{inner:.2f},{inner:.2f} 0 {large_arc} 0 {start_inner[0]:.2f},{start_inner[1]:.2f} Z"
    )
    return f'<path d="{path}" fill="{color}" stroke="#ffffff" stroke-width="1"><title>{esc(tooltip)}</title></path>'


def _polar(cx: float, cy: float, radius: float, angle_degrees: float) -> tuple[float, float]:
    radians = math.radians(angle_degrees)
    return cx + radius * math.cos(radians), cy + radius * math.sin(radians)
