from __future__ import annotations

from datetime import datetime

import moneo


def test_parse_due_time_only() -> None:
    at, date = moneo.parse_due_string("16:15")
    assert at == "16:15"
    assert date is None


def test_parse_due_date_only() -> None:
    at, date = moneo.parse_due_string("2026-03-16")
    assert at is None
    assert date == "2026-03-16"


def test_parse_due_today_with_time() -> None:
    now = datetime(2026, 3, 16, 9, 0, tzinfo=moneo.HKT)
    at, date = moneo.parse_due_string("today 10:00")
    assert at == "10:00"
    assert date == moneo.resolve_date_keyword("today")


def test_parse_due_tomorrow_with_time() -> None:
    at, date = moneo.parse_due_string("tomorrow 09:00")
    assert at == "09:00"
    assert date == moneo.resolve_date_keyword("tomorrow")


def test_parse_due_iso_date_with_time() -> None:
    at, date = moneo.parse_due_string("2026-12-25 14:30")
    assert at == "14:30"
    assert date == "2026-12-25"


def test_parse_due_today_keyword_alone() -> None:
    at, date = moneo.parse_due_string("today")
    assert at is None
    assert date == moneo.resolve_date_keyword("today")


def test_parse_due_tomorrow_keyword_alone() -> None:
    at, date = moneo.parse_due_string("tomorrow")
    assert at is None
    assert date == moneo.resolve_date_keyword("tomorrow")


def test_resolve_date_keyword_today_case_insensitive() -> None:
    now = datetime(2026, 3, 16, 9, 0, tzinfo=moneo.HKT)
    today = "2026-03-16"
    assert moneo.resolve_date_keyword("today", now=now) == today
    assert moneo.resolve_date_keyword("Today", now=now) == today
    assert moneo.resolve_date_keyword("TODAY", now=now) == today


def test_resolve_date_keyword_passthrough() -> None:
    assert moneo.resolve_date_keyword("2026-03-16") == "2026-03-16"


def test_parse_time_with_date_defaults_to_0900() -> None:
    now = datetime(2026, 3, 16, 8, 0, tzinfo=moneo.HKT)
    ts = moneo.parse_time(None, None, "2026-03-20", now=now)
    assert moneo.hkt_from_ts(ts) == datetime(2026, 3, 20, 9, 0, tzinfo=moneo.HKT)


def test_expand_schedule_skips_night_by_default() -> None:
    base = int(datetime(2026, 3, 16, 9, 0, tzinfo=moneo.HKT).timestamp())
    expanded = moneo.expand_schedule(base, moneo.parse_interval("6h"), "2026-03-17")
    formatted = [moneo.hkt_from_ts(ts).strftime("%Y-%m-%d %H:%M") for ts in expanded]
    assert formatted == [
        "2026-03-16 09:00",
        "2026-03-16 15:00",
        "2026-03-16 21:00",
        "2026-03-17 09:00",
        "2026-03-17 15:00",
        "2026-03-17 21:00",
    ]


def test_expand_schedule_can_include_night() -> None:
    base = int(datetime(2026, 3, 16, 9, 0, tzinfo=moneo.HKT).timestamp())
    expanded = moneo.expand_schedule(
        base,
        moneo.parse_interval("6h"),
        "2026-03-17",
        skip_night=False,
    )
    formatted = [moneo.hkt_from_ts(ts).strftime("%Y-%m-%d %H:%M") for ts in expanded]
    assert formatted == [
        "2026-03-16 09:00",
        "2026-03-16 15:00",
        "2026-03-16 21:00",
        "2026-03-17 03:00",
        "2026-03-17 09:00",
        "2026-03-17 15:00",
        "2026-03-17 21:00",
    ]
