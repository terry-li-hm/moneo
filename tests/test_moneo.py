from __future__ import annotations

import base64
import copy
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


# --- recur_code ---


def test_recur_code_all_frequencies() -> None:
    assert moneo.recur_code("daily") == "d"
    assert moneo.recur_code("weekly") == "w"
    assert moneo.recur_code("monthly") == "m"
    assert moneo.recur_code("quarterly") == "q"
    assert moneo.recur_code("yearly") == "y"


def test_recur_code_unknown_returns_none() -> None:
    assert moneo.recur_code("hourly") is None
    assert moneo.recur_code("") is None


# --- generate_uuid ---


def test_generate_uuid_format() -> None:
    uid = moneo.generate_uuid()
    assert isinstance(uid, str)
    assert len(uid) == 22  # base64url of 16 bytes, no padding
    assert "=" not in uid
    # Should decode back to 16 bytes
    raw = base64.urlsafe_b64decode(uid + "==")
    assert len(raw) == 16


def test_generate_uuid_uniqueness() -> None:
    uuids = {moneo.generate_uuid() for _ in range(100)}
    assert len(uuids) == 100


# --- make_reminder ---


def test_make_reminder_basic() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    reminder = moneo.make_reminder("Test reminder", ts, None, None)
    assert reminder["n"] == "Test reminder"
    assert reminder["d"] == ts
    assert reminder["si"] == 300  # default 5 min autosnooze
    assert "u" in reminder
    assert len(reminder["u"]) == 22
    assert "b" in reminder  # created timestamp
    assert "m" in reminder  # modified timestamp
    assert "rf" not in reminder  # no recurrence


def test_make_reminder_with_autosnooze() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    reminder = moneo.make_reminder("Test", ts, None, 15)
    assert reminder["si"] == 900  # 15 * 60


def test_make_reminder_daily_recurrence() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    reminder = moneo.make_reminder("Daily", ts, "daily", None)
    assert reminder["rf"] == "d"
    assert reminder["rd"] == ts
    assert reminder["rn"] == 16  # daily unit


def test_make_reminder_weekly_recurrence() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    reminder = moneo.make_reminder("Weekly", ts, "weekly", None)
    assert reminder["rf"] == "w"
    assert reminder["rd"] == ts
    assert reminder["rn"] == 256  # weekly unit
    assert "rb" in reminder  # weekday byday


def test_make_reminder_quarterly_recurrence() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    reminder = moneo.make_reminder("Quarterly", ts, "quarterly", None)
    assert reminder["rf"] == "q"
    assert reminder["ru"] == {"i": 3}  # interval 3 months


# --- add_direct ---


def _empty_db() -> dict:
    return {"re": [], "mt": {"ts": 0}, "dl": {}}


def test_add_direct_appends_to_data() -> None:
    data = _empty_db()
    uid = moneo.add_direct("Test", 1000000, None, None, data)
    assert len(data["re"]) == 1
    assert data["re"][0]["u"] == uid
    assert data["re"][0]["n"] == "Test"


def test_add_direct_multiple() -> None:
    data = _empty_db()
    moneo.add_direct("First", 1000000, None, None, data)
    moneo.add_direct("Second", 2000000, None, None, data)
    assert len(data["re"]) == 2
    titles = {r["n"] for r in data["re"]}
    assert titles == {"First", "Second"}


def test_add_direct_preserves_existing() -> None:
    data = _empty_db()
    data["re"].append({"u": "existing123456789012", "n": "Existing", "d": 500000})
    moneo.add_direct("New", 1000000, None, None, data)
    assert len(data["re"]) == 2
    assert data["re"][0]["n"] == "Existing"
    assert data["re"][1]["n"] == "New"


# --- find_duplicate ---


def test_find_duplicate_detects_same_title_and_time() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    data = _empty_db()
    data["re"].append({"u": "abc", "n": "Med reminder", "d": ts})
    result = moneo.find_duplicate("Med reminder", ts, data)
    assert result is not None


def test_find_duplicate_case_insensitive() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    data = _empty_db()
    data["re"].append({"u": "abc", "n": "Med Reminder", "d": ts})
    result = moneo.find_duplicate("med reminder", ts, data)
    assert result is not None


def test_find_duplicate_different_time_no_match() -> None:
    ts1 = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    ts2 = int(datetime(2026, 3, 20, 14, 0, tzinfo=moneo.HKT).timestamp())
    data = _empty_db()
    data["re"].append({"u": "abc", "n": "Med reminder", "d": ts1})
    result = moneo.find_duplicate("Med reminder", ts2, data)
    assert result is None


def test_find_duplicate_different_title_no_match() -> None:
    ts = int(datetime(2026, 3, 20, 10, 0, tzinfo=moneo.HKT).timestamp())
    data = _empty_db()
    data["re"].append({"u": "abc", "n": "Med reminder", "d": ts})
    result = moneo.find_duplicate("Different", ts, data)
    assert result is None
