"""История запусков в SQLite: база для сравнения и аномалий."""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .models import DailyData


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    def _create_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    day          TEXT NOT NULL,
                    cluster_id   TEXT NOT NULL DEFAULT '',
                    period_from  TEXT NOT NULL,
                    period_to    TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    total        INTEGER NOT NULL,
                    unique_rules INTEGER NOT NULL,
                    unique_sources INTEGER,
                    base_events  INTEGER,
                    PRIMARY KEY (day, cluster_id)
                );

                CREATE TABLE IF NOT EXISTS rule_daily (
                    day        TEXT NOT NULL,
                    cluster_id TEXT NOT NULL DEFAULT '',
                    rule_name  TEXT NOT NULL,
                    rule_id    TEXT NOT NULL DEFAULT '',
                    count      INTEGER NOT NULL,
                    severity   TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (day, cluster_id, rule_name)
                );

                CREATE INDEX IF NOT EXISTS idx_rule_daily_lookup
                    ON rule_daily (cluster_id, day);
                """
            )

    def save_day(self, data: DailyData) -> None:
        if not data.day:
            raise ValueError("Не задан день отчёта — сохранять нечего")
        cluster_id = data.cluster_id or ""
        unique_sources = _raw_unique(data.raw, "SourceAddress")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO runs (
                    day, cluster_id, period_from, period_to, generated_at,
                    total, unique_rules, unique_sources, base_events
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(day, cluster_id) DO UPDATE SET
                    period_from=excluded.period_from,
                    period_to=excluded.period_to,
                    generated_at=excluded.generated_at,
                    total=excluded.total,
                    unique_rules=excluded.unique_rules,
                    unique_sources=excluded.unique_sources,
                    base_events=excluded.base_events
                """,
                (
                    data.day,
                    cluster_id,
                    data.period_from,
                    data.period_to,
                    data.generated_at,
                    int(data.total),
                    len(data.rules),
                    unique_sources,
                    data.base_events,
                ),
            )
            self.conn.execute(
                "DELETE FROM rule_daily WHERE day = ? AND cluster_id = ?",
                (data.day, cluster_id),
            )
            self.conn.executemany(
                """
                INSERT INTO rule_daily (day, cluster_id, rule_name, rule_id, count, severity)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        data.day,
                        cluster_id,
                        rule.name,
                        rule.rule_id or "",
                        int(rule.count),
                        rule.severity or "",
                    )
                    for rule in data.rules
                    if rule.name
                ],
            )

    def totals(
        self,
        cluster_id: str,
        before_day: str,
        window_days: int,
    ) -> list[tuple[str, int]]:
        cutoff = _cutoff(before_day, window_days)
        rows = self.conn.execute(
            """
            SELECT day, total FROM runs
            WHERE cluster_id = ? AND day < ? AND day >= ?
            ORDER BY day ASC
            """,
            (cluster_id or "", before_day, cutoff),
        ).fetchall()
        return [(str(row["day"]), int(row["total"])) for row in rows]

    def rule_counts(
        self,
        cluster_id: str,
        before_day: str,
        window_days: int,
    ) -> dict[str, dict[str, int]]:
        cutoff = _cutoff(before_day, window_days)
        rows = self.conn.execute(
            """
            SELECT day, rule_name, count FROM rule_daily
            WHERE cluster_id = ? AND day < ? AND day >= ?
            ORDER BY day ASC
            """,
            (cluster_id or "", before_day, cutoff),
        ).fetchall()
        result: dict[str, dict[str, int]] = {}
        for row in rows:
            day = str(row["day"])
            result.setdefault(day, {})[str(row["rule_name"])] = int(row["count"])
        return result

    def last_day(self, cluster_id: str, before_day: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT day FROM runs
            WHERE cluster_id = ? AND day < ?
            ORDER BY day DESC LIMIT 1
            """,
            (cluster_id or "", before_day),
        ).fetchone()
        return str(row["day"]) if row else None

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "HistoryStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _cutoff(before_day: str, window_days: int) -> str:
    target = date.fromisoformat(before_day) - timedelta(days=max(1, int(window_days)))
    return target.isoformat()


def _raw_unique(raw: dict[str, Any], field: str) -> int | None:
    unique = raw.get("unique") if isinstance(raw, dict) else None
    if not isinstance(unique, dict):
        return None
    value = unique.get(field)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
