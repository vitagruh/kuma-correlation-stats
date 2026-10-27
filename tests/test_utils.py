from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from kuma_stats.utils import (
    day_bounds,
    fmt_int,
    fmt_pct,
    hour_slices,
    inclusive_period,
    load_timezone,
    parse_dt,
    parse_dt as parse,
    severity_color,
    severity_label,
    to_rfc3339,
)


class TimeTests(unittest.TestCase):
    def test_parse_rfc3339_and_epoch(self):
        parsed = parse_dt("2026-10-06T09:00:00Z")
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 9)
        self.assertEqual(parse_dt(1_791_000_000).year, 2026)
        self.assertEqual(parse_dt("1791000000000").year, 2026)
        self.assertIsNone(parse_dt("not a date"))

    def test_day_bounds_respect_timezone(self):
        tz, _warning = load_timezone("Europe/Moscow", 3.0)
        start, end = day_bounds(date(2026, 10, 6), tz)
        self.assertEqual((end - start), timedelta(days=1))
        self.assertEqual(start.astimezone(timezone.utc).hour, 21)
        self.assertEqual(start.astimezone(timezone.utc).day, 5)

    def test_inclusive_period_shifts_end(self):
        start = datetime(2026, 10, 6, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        period_from, period_to = inclusive_period(start, end)
        self.assertEqual(period_from, "2026-10-06T00:00:00.000Z")
        self.assertEqual(period_to, "2026-10-06T23:59:59.999Z")

    def test_hour_slices_cover_full_day(self):
        start = datetime(2026, 10, 6, tzinfo=timezone.utc)
        slices = hour_slices(start, start + timedelta(days=1))
        self.assertEqual(len(slices), 24)
        self.assertEqual(slices[0][0], start)
        self.assertEqual(slices[-1][1], start + timedelta(days=1))

    def test_fixed_offset_fallback(self):
        tz, warning = load_timezone("Not/AZone", 3.0)
        self.assertTrue(warning)
        self.assertEqual(tz.utcoffset(None), timedelta(hours=3))


class FormatTests(unittest.TestCase):
    def test_numbers_use_thin_spaces(self):
        self.assertEqual(fmt_int(1234567), "1\u2009234\u2009567")
        self.assertEqual(fmt_pct(12.345), "12,3 %")
        self.assertEqual(fmt_pct(10, signed=True), "+10,0 %")
        self.assertEqual(fmt_pct(-3.2, signed=True), "-3,2 %")

    def test_severity_mapping(self):
        self.assertEqual(severity_label("critical"), "Критический")
        self.assertEqual(severity_label("3"), "Высокий")
        self.assertEqual(severity_color("high"), "#ea580c")
        self.assertEqual(severity_color("неизвестно"), "#94a3b8")

    def test_rfc3339_roundtrip(self):
        value = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
        text = to_rfc3339(value)
        self.assertEqual(parse(text), value)


if __name__ == "__main__":
    unittest.main()
