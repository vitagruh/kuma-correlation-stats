"""Вспомогательные функции: время, форматирование, файлы, HTML."""
from __future__ import annotations

import csv
import html
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# --------------------------------------------------------------------------- #
# Время
# --------------------------------------------------------------------------- #

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_timezone(name: str, fallback_offset_hours: float | None) -> tuple[tzinfo, str]:
    """Возвращает таймзону и предупреждение (пустая строка, если всё хорошо).

    На Windows база IANA может отсутствовать без пакета ``tzdata``. В этом
    случае используется фиксированный сдвиг из конфига.
    """
    name = (name or "").strip()
    if name.upper() in {"UTC", "Z"}:
        return timezone.utc, ""
    try:
        return ZoneInfo(name), ""
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        if fallback_offset_hours is None:
            raise ValueError(
                f"Часовой пояс '{name}' недоступен. Установите пакет tzdata "
                "или задайте report.utc_offset_hours."
            )
        tz = timezone(timedelta(hours=float(fallback_offset_hours)))
        warning = (
            f"Часовой пояс '{name}' недоступен (нет базы IANA). "
            f"Использован фиксированный сдвиг UTC{_offset_label(fallback_offset_hours)}. "
            "Для корректного перехода на летнее время установите пакет tzdata."
        )
        return tz, warning


def _offset_label(hours: float) -> str:
    sign = "+" if hours >= 0 else "-"
    hours = abs(float(hours))
    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    return f"{sign}{whole:02d}:{minutes:02d}"


def to_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_dt(value: object) -> datetime | None:
    """Разбирает RFC3339-строку или epoch (секунды/миллисекунды)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if abs(number) >= 1e11:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        try:
            return parse_dt(float(text))
        except ValueError:
            return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Границы локальных суток в UTC. Верхняя граница не включается."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def hour_slices(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Режет интервал на часовые срезы (последний может быть короче)."""
    result: list[tuple[datetime, datetime]] = []
    cursor = start
    step = timedelta(hours=1)
    while cursor < end:
        slice_end = min(cursor + step, end)
        result.append((cursor, slice_end))
        cursor = slice_end
    return result


def inclusive_period(start: datetime, end: datetime) -> tuple[str, str]:
    """KUMA включает обе границы периода, поэтому сдвигаем конец на 1 мс."""
    safe_end = end - timedelta(milliseconds=1)
    if safe_end < start:
        safe_end = start
    return to_rfc3339(start), to_rfc3339(safe_end)


def hour_label(local_dt: datetime) -> str:
    return local_dt.strftime("%H:%M")


def fmt_date(value: str) -> str:
    parsed = parse_dt(value)
    if not parsed:
        return value or "—"
    return parsed.strftime("%d.%m.%Y %H:%M UTC")


# --------------------------------------------------------------------------- #
# Форматирование чисел
# --------------------------------------------------------------------------- #

def fmt_int(value: object) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,}".replace(",", "\u2009")


def fmt_float(value: object, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isinf(number):
        return "∞"
    if math.isnan(number):
        return "—"
    return f"{number:,.{digits}f}".replace(",", "\u2009").replace(".", ",")


def fmt_pct(value: object, digits: int = 1, signed: bool = False) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isinf(number):
        return "∞"
    if math.isnan(number):
        return "—"
    sign = "+" if signed and number > 0 else ""
    return f"{sign}{number:,.{digits}f}".replace(",", "\u2009").replace(".", ",") + " %"


def fmt_duration(seconds: float) -> str:
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f} с"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes} мин {sec} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин"


def pct_delta(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base == 0:
        return None
    return (float(current) - float(base)) / float(base) * 100.0


def safe_div(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return float(numerator) / float(denominator)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pstdev(values: list[float]) -> float | None:
    if not values:
        return None
    avg = sum(values) / len(values)
    return math.sqrt(sum((v - avg) ** 2 for v in values) / len(values))


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


# --------------------------------------------------------------------------- #
# Критичность
# --------------------------------------------------------------------------- #

SEVERITY_COLORS = {
    "critical": "#dc2626",
    "критический": "#dc2626",
    "high": "#ea580c",
    "высокий": "#ea580c",
    "medium": "#d97706",
    "средний": "#d97706",
    "low": "#0284c7",
    "низкий": "#0284c7",
    "info": "#64748b",
    "informational": "#64748b",
    "информационный": "#64748b",
}

_SEVERITY_ORDER = {
    "info": 0,
    "informational": 0,
    "информационный": 0,
    "low": 1,
    "низкий": 1,
    "medium": 2,
    "средний": 2,
    "high": 3,
    "высокий": 3,
    "critical": 4,
    "критический": 4,
}

_SEVERITY_RU = {
    "info": "Инфо",
    "informational": "Инфо",
    "low": "Низкий",
    "medium": "Средний",
    "high": "Высокий",
    "critical": "Критический",
    "информационный": "Инфо",
    "низкий": "Низкий",
    "средний": "Средний",
    "высокий": "Высокий",
    "критический": "Критический",
}


_SEVERITY_NUMERIC = {
    "0": "info",
    "1": "low",
    "2": "medium",
    "3": "high",
    "4": "critical",
}


def severity_key(value: str) -> str:
    key = str(value or "").strip().lower()
    return _SEVERITY_NUMERIC.get(key, key)


def severity_color(value: str) -> str:
    return SEVERITY_COLORS.get(severity_key(value), "#94a3b8")


def severity_rank(value: str) -> int:
    return _SEVERITY_ORDER.get(severity_key(value), -1)


def severity_label(value: str) -> str:
    if not value:
        return "—"
    key = severity_key(value)
    return _SEVERITY_RU.get(key, value)


# --------------------------------------------------------------------------- #
# Файлы и HTML
# --------------------------------------------------------------------------- #

_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def is_valid_field_name(name: str) -> bool:
    return bool(_FIELD_RE.fullmatch(name or ""))


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("._")
    return cleaned or "data"


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_json(path: str | os.PathLike[str], payload: object) -> None:
    ensure_dir(Path(path).parent)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def read_json(path: str | os.PathLike[str]) -> object:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_csv(
    path: str | os.PathLike[str],
    headers: list[str],
    rows: list[list[object]],
) -> None:
    ensure_dir(Path(path).parent)
    # utf-8-sig и «;» — чтобы Excel с русской локалью открывал без плясок.
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(headers)
        for row in rows:
            writer.writerow(["" if value is None else value for value in row])


def esc(value: object) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def truncate(value: str, limit: int = 60) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def plural_ru(number: int, one: str, few: str, many: str) -> str:
    number = abs(int(number))
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many
