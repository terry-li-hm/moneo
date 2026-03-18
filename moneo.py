#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import argparse
import copy
import gzip
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote
from zoneinfo import ZoneInfo

HKT = ZoneInfo("Asia/Hong_Kong")
VALID_AUTOSNOOZE = {1, 5, 10, 15, 30, 60}
RECUR_CHOICES = {"daily", "weekly", "monthly", "quarterly", "yearly"}
APPLE_SCRIPT = r"""
tell application "System Events"
    tell process "Due"
        repeat 20 times
            try
                click button "Save" of window "Reminder Editor"
                return "ok"
            end try
            delay 0.5
        end repeat
    end tell
end tell
return "timeout"
""".strip()


class MoneoError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChangeSet:
    title: str
    due_ts: int
    changed: list[str]
    recur: str | None


def fatal(message: str) -> NoReturn:
    raise MoneoError(message)


def home_dir() -> Path:
    home = os.environ.get("HOME")
    if not home:
        fatal("HOME is not set")
    return Path(home)


def due_db_path() -> Path:
    return home_dir() / "Library/Containers/com.phocusllp.duemac/Data/Library/Application Support/Due App/Due.duedb"


def snapshot_path() -> Path:
    return home_dir() / "officina/backups/due-reminders.json"


def log_path() -> Path:
    return home_dir() / "tmp/due-snapshot.log"


def hkt_now() -> datetime:
    return datetime.now(HKT)


def now_ts() -> int:
    return int(datetime.now(UTC).timestamp())


def hkt_from_ts(ts: int, timezone: str = "Asia/Hong_Kong") -> datetime:
    try:
        tz = ZoneInfo(timezone)
    except Exception as exc:  # pragma: no cover - defensive
        fatal(f"Invalid timezone '{timezone}': {exc}")
    return datetime.fromtimestamp(ts, tz=UTC).astimezone(tz)


def parse_due_string(due: str) -> tuple[str | None, str | None]:
    parts = due.split()
    if len(parts) == 1:
        part = parts[0]
        if ":" in part:
            return part, None
        return None, resolve_date_keyword(part)
    if len(parts) == 2:
        return parts[1], resolve_date_keyword(parts[0])
    fatal(
        f"Error: invalid --due '{due}'. Use e.g. 'today 16:15', 'tomorrow', '2026-03-16 10:00'."
    )


def resolve_date_keyword(value: str, now: datetime | None = None) -> str:
    now = now or hkt_now()
    lowered = value.lower()
    if lowered == "today":
        return now.strftime("%Y-%m-%d")
    if lowered == "tomorrow":
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return value


def parse_relative(value: str) -> timedelta:
    if len(value) < 2:
        fatal(f"Error: invalid --in '{value}'. Use e.g. 30m, 2h, 90s.")
    amount, unit = value[:-1], value[-1]
    try:
        n = int(amount)
    except ValueError:
        fatal(f"Error: invalid --in '{value}'. Use e.g. 30m, 2h, 90s.")
    if unit == "s":
        return timedelta(seconds=n)
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    fatal(f"Error: invalid --in '{value}'. Use e.g. 30m, 2h, 90s.")


def parse_interval(value: str) -> timedelta:
    if len(value) < 2:
        fatal(f"Error: invalid --every '{value}'. Use Nh, Nm, or Nd.")
    amount, unit = value[:-1], value[-1]
    try:
        n = int(amount)
    except ValueError:
        fatal(f"Error: invalid --every '{value}'. Use Nh, Nm, or Nd.")
    if n <= 0:
        fatal(f"Error: invalid --every '{value}'. Use a positive interval.")
    if unit == "m":
        return timedelta(minutes=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "d":
        return timedelta(days=n)
    fatal(f"Error: invalid --every '{value}'. Use Nh, Nm, or Nd.")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        fatal(f"Error: invalid --date '{value}'. Use YYYY-MM-DD.")


def parse_at(value: str) -> dt_time:
    for fmt in ("%H:%M", "%-H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            return dt_time(parsed.hour, parsed.minute)
        except ValueError:
            continue
    fatal(f"Error: invalid --at '{value}'. Use HH:MM.")


def resolve_local_timestamp(day: date, at_value: str | None, timezone: str) -> int:
    zone = ZoneInfo(timezone)
    if at_value is None:
        local = datetime.combine(day, dt_time(9, 0), tzinfo=zone)
        return int(local.timestamp())
    parsed_time = parse_at(at_value)
    local = datetime.combine(day, parsed_time, tzinfo=zone)
    return int(local.timestamp())


def parse_time(
    rel: str | None,
    at: str | None,
    date_value: str | None,
    *,
    timezone: str = "Asia/Hong_Kong",
    now: datetime | None = None,
) -> int | None:
    now = now or datetime.now(ZoneInfo(timezone))
    if rel:
        return int((now + parse_relative(rel)).timestamp())
    if date_value:
        return resolve_local_timestamp(parse_date(date_value), at, timezone)
    if at:
        return resolve_local_timestamp(now.date(), at, timezone)
    return None


def fmt_ts(ts: int) -> str:
    dt = hkt_from_ts(ts)
    now = hkt_now()
    if dt.date() == now.date():
        return dt.strftime("today %H:%M")
    if dt.date() == (now + timedelta(days=1)).date():
        return dt.strftime("tomorrow %H:%M")
    return dt.strftime("%b %d %H:%M")


def read_db() -> dict[str, Any]:
    path = due_db_path()
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except PermissionError:
        return {}
    except FileNotFoundError as exc:
        fatal(f"Failed to read {path}: {exc}")
    except OSError as exc:
        fatal(f"Failed to parse {path}: {exc}")
    except json.JSONDecodeError as exc:
        fatal(f"Failed to parse {path}: {exc}")


def due_pid() -> str | None:
    result = subprocess.run(["pgrep", "-x", "Due"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    pid = result.stdout.strip()
    return pid or None


def run_best_effort(program: str, *args: str) -> None:
    subprocess.run(
        [program, *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def reminders_slice(data: dict[str, Any]) -> list[dict[str, Any]]:
    reminders = data.get("re")
    return reminders if isinstance(reminders, list) else []


def active_reminders(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return reminders excluding entries that appear in the logbook (completed).

    Cross-references against data["lb"] using (title, due_ts) tuples.  Logbook
    entries use "n" for title and "d" for the original due timestamp (same field
    names as reminder entries).  If a logbook entry lacks "d", it is matched by
    title alone to stay conservative.
    """
    logbook = data.get("lb")
    if not isinstance(logbook, list) or not logbook:
        return reminders_slice(data)

    # Build completed sets: prefer (title, due_ts) pairs; fall back to title-only
    # for logbook entries that lack a "d" field.
    completed_pairs: set[tuple[str, int]] = set()
    completed_titles_only: set[str] = set()
    for entry in logbook:
        title = str(entry.get("n", "")).strip().lower()
        if not title:
            continue
        due_ts = entry.get("d")
        if isinstance(due_ts, int):
            completed_pairs.add((title, due_ts))
        else:
            completed_titles_only.add(title)

    active: list[dict[str, Any]] = []
    for reminder in reminders_slice(data):
        title = reminder_title(reminder).strip().lower()
        due_ts = reminder_due_ts(reminder)
        if due_ts is not None and (title, due_ts) in completed_pairs:
            continue
        if due_ts is None and title in completed_titles_only:
            continue
        active.append(reminder)
    return active


def reminders_mut(data: dict[str, Any]) -> list[dict[str, Any]]:
    reminders = data.setdefault("re", [])
    if not isinstance(reminders, list):
        fatal("Due DB field 're' is not an array")
    return reminders


def reminder_due_ts(reminder: dict[str, Any]) -> int | None:
    due = reminder.get("d")
    return due if isinstance(due, int) else None


def reminder_title(reminder: dict[str, Any]) -> str:
    title = reminder.get("n")
    return title if isinstance(title, str) else ""


def reminder_uuid(reminder: dict[str, Any]) -> str | None:
    value = reminder.get("u")
    return value if isinstance(value, str) else None


def sorted_reminders(data: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(reminders_slice(data), key=lambda reminder: reminder_due_ts(reminder) or 0)


def recur_label(code: str | None) -> str | None:
    return {
        "d": "daily",
        "w": "weekly",
        "m": "monthly",
        "q": "quarterly",
        "y": "yearly",
    }.get(code or "")


def recur_unit(freq: str) -> int | None:
    return {
        "daily": 16,
        "weekly": 256,
        "monthly": 8,
        "quarterly": 8,
        "yearly": 4,
    }.get(freq)


def recur_freq(freq: str) -> int | None:
    return {
        "daily": 1,
        "weekly": 1,
        "monthly": 1,
        "quarterly": 3,
        "yearly": 1,
    }.get(freq)


def find_duplicate(title: str, due_ts: int, data: dict[str, Any] | None = None) -> dict[str, Any] | None:
    due_dt = hkt_from_ts(due_ts)
    normalized = title.strip().lower()
    haystack = active_reminders(data or read_db())
    for reminder in haystack:
        if reminder_title(reminder).strip().lower() != normalized:
            continue
        existing_due = reminder_due_ts(reminder)
        if existing_due is None:
            continue
        existing_dt = hkt_from_ts(existing_due)
        if (
            existing_dt.date() == due_dt.date()
            and existing_dt.hour == due_dt.hour
            and existing_dt.minute == due_dt.minute
        ):
            return reminder
    return None


def make_snapshot(data: dict[str, Any]) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for reminder in sorted_reminders(data):
        due_ts = reminder_due_ts(reminder)
        snapshot.append(
            {
                "title": reminder_title(reminder),
                "due": fmt_ts(due_ts) if due_ts is not None else None,
                "due_ts": due_ts,
                "recur": recur_label(reminder.get("rf") if isinstance(reminder.get("rf"), str) else None),
                "uuid": reminder_uuid(reminder),
            }
        )
    return snapshot


def comparable_snapshot(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "title": reminder_title(reminder),
            "due": reminder_due_ts(reminder),
            "recur": recur_label(reminder.get("rf") if isinstance(reminder.get("rf"), str) else None),
        }
        for reminder in sorted_reminders(data)
    ]


def git_snapshot(data: dict[str, Any]) -> None:
    snapshot = make_snapshot(data)
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(snapshot, indent=2)}\n", encoding="utf-8")

    repo = path.parent
    subprocess.run(["git", "-C", str(repo), "add", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", f"due: snapshot ({len(snapshot)} reminders)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def log_line(message: str) -> None:
    line = f"[{hkt_now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(line, end="")
    sys.stdout.flush()


def write_db(data: dict[str, Any]) -> None:
    due_db = due_db_path()
    backup = due_db.with_suffix(".duedb.bak")
    try:
        backup.write_bytes(due_db.read_bytes())
    except OSError as exc:
        fatal(f"Failed to back up {due_db} to {backup}: {exc}")

    pid = due_pid()
    if pid:
        run_best_effort("kill", "-15", pid)
        for _ in range(20):
            time.sleep(0.2)
            if not due_pid():
                break

    try:
        with gzip.open(due_db, "wt", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
    except Exception as exc:
        try:
            due_db.write_bytes(backup.read_bytes())
        except OSError as restore_exc:
            fatal(f"Write failed, and restore also failed for {due_db}: {restore_exc}")
        fatal(f"Write failed, restored backup: {exc}")

    if pid:
        run_best_effort("open", "-a", "Due")

    git_snapshot(data)


def set_tombstone(data: dict[str, Any], uuid: str, ts: int) -> None:
    dl = data.setdefault("dl", {})
    if not isinstance(dl, dict):
        fatal("Due DB field 'dl' is not an object")
    dl[uuid] = ts


def get_reminder(data: dict[str, Any], index: int) -> tuple[int, dict[str, Any]]:
    reminders = sorted_reminders(data)
    if index <= 0 or index > len(reminders):
        fatal(f"Error: no reminder at index {index}.")
    target = reminders[index - 1]
    target_uuid = reminder_uuid(target)
    if not target_uuid:
        fatal("Reminder is missing UUID")
    raw = reminders_slice(data)
    for raw_idx, reminder in enumerate(raw):
        if reminder_uuid(reminder) == target_uuid:
            return raw_idx, reminder
    fatal("Reminder UUID not found in raw reminder list")


def sync_via_applescript(
    title: str,
    due_ts: int,
    recur: str | None,
    *,
    timezone: str,
    autosnooze: int | None,
) -> bool:
    url = f"due:///add?title={quote(title)}&duedate={due_ts}&timezone={quote(timezone)}"
    if autosnooze is not None:
        url += f"&autosnooze={autosnooze}"
    if recur:
        unit = recur_unit(recur)
        freq = recur_freq(recur)
        if unit and freq:
            url += f"&recurunit={unit}&recurfreq={freq}&recurfromdate={due_ts}"
            if recur == "weekly":
                dt = hkt_from_ts(due_ts, timezone)
                weekday = ((dt.weekday() + 1) % 7) + 1
                url += f"&recurbyday={weekday}"

    run_best_effort("caffeinate", "-u", "-t", "1")
    time.sleep(0.5)
    run_best_effort("open", url)
    time.sleep(3)
    result = subprocess.run(
        ["osascript", "-e", APPLE_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
    )
    return "ok" in result.stdout


def cmd_ls(_: argparse.Namespace) -> None:
    data = read_db()
    reminders = sorted_reminders(data)
    if not reminders:
        print("No reminders.")
        return
    now = now_ts()
    print(f"{'#':<4} {'Title':<36} {'Due':<16} Recur")
    print("─" * 68)
    for index, reminder in enumerate(reminders, start=1):
        title = reminder_title(reminder)[:35]
        due = reminder_due_ts(reminder) or 0
        due_str = "—" if due == 0 else fmt_ts(due)
        flag = " ⚠" if due and due < now else ""
        recur = recur_label(reminder.get("rf") if isinstance(reminder.get("rf"), str) else None) or ""
        print(f"{index:<4} {title:<36} {(due_str + flag):<16} {recur}")


def validate_add_args(args: argparse.Namespace) -> tuple[str | None, str | None]:
    if args.recur and args.recur not in RECUR_CHOICES:
        fatal(f"Error: invalid --recur '{args.recur}'.")
    if args.autosnooze is not None and args.autosnooze not in VALID_AUTOSNOOZE:
        fatal("Error: --autosnooze must be one of 1, 5, 10, 15, 30, 60.")
    if args.every and not args.until:
        fatal("Error: --every requires --until.")
    if args.until and not args.every:
        fatal("Error: --until requires --every.")
    if args.every and args.recur:
        fatal("Error: --every cannot be combined with --recur.")
    if args.every and args.rel:
        fatal("Error: --every cannot be combined with --in.")
    if args.due and (args.at or args.date):
        fatal("Error: --due cannot be combined with --at or --date.")
    if args.due:
        return parse_due_string(args.due)
    return args.at, args.date


def expand_schedule(
    base_ts: int,
    interval: timedelta,
    until_value: str,
    *,
    skip_night: bool = True,
    timezone: str = "Asia/Hong_Kong",
) -> list[int]:
    until_day = parse_date(until_value)
    zone = ZoneInfo(timezone)
    current = datetime.fromtimestamp(base_ts, tz=zone)
    end_of_day = datetime.combine(until_day, dt_time(23, 59, 59), tzinfo=zone)
    results: list[int] = []
    while current <= end_of_day:
        include = True
        if skip_night and (current.hour >= 23 or current.hour < 7):
            include = False
        if include:
            results.append(int(current.timestamp()))
        current += interval
    return results


def ensure_no_duplicates(title: str, due_times: list[int], data: dict[str, Any]) -> None:
    for due_ts in due_times:
        duplicate = find_duplicate(title, due_ts, data)
        if duplicate:
            existing_due = reminder_due_ts(duplicate) or due_ts
            fatal(
                f"Duplicate: '{title}' already exists on that day at {fmt_ts(existing_due)}. "
                "Use 'moneo edit' to change the time."
            )


def add_one(title: str, due_ts: int, recur: str | None, autosnooze: int | None, timezone: str) -> bool:
    return sync_via_applescript(title, due_ts, recur, timezone=timezone, autosnooze=autosnooze)


def cmd_add(args: argparse.Namespace) -> None:
    at_value, date_value = validate_add_args(args)
    due_ts = parse_time(args.rel, at_value, date_value, timezone=args.timezone)
    if due_ts is None:
        fatal("Error: specify a time with --in, --at, --date, or --due.")

    due_times = [due_ts]
    if args.every:
        due_times = expand_schedule(
            due_ts,
            parse_interval(args.every),
            args.until,
            skip_night=not args.no_skip_night,
            timezone=args.timezone,
        )
        if not due_times:
            fatal("Error: expanded schedule produced no reminders.")

    db = read_db()
    ensure_no_duplicates(args.title, due_times, db)

    successes = 0
    for scheduled_ts in due_times:
        if add_one(args.title, scheduled_ts, args.recur, args.autosnooze, args.timezone):
            successes += 1
        else:
            print("Due editor open — please click Save manually to sync to iPhone.")
            return

    recur_str = f" (repeats {args.recur})" if args.recur else ""
    if len(due_times) == 1:
        print(
            f"Added: '{args.title}' due {fmt_ts(due_times[0])}{recur_str} — synced to iPhone via CloudKit (AppleScript)"
        )
    else:
        print(
            f"Added {successes} reminders for '{args.title}' from {fmt_ts(due_times[0])} to {fmt_ts(due_times[-1])} "
            "— synced to iPhone via CloudKit (AppleScript)"
        )
    git_snapshot(read_db())


def cmd_rm(args: argparse.Namespace) -> None:
    data = read_db()
    pattern = args.pattern.strip().lower()
    matches = [copy.deepcopy(reminder) for reminder in reminders_slice(data) if pattern in reminder_title(reminder).lower()]
    if not matches:
        fatal(f"No reminders matching '{args.pattern}'.")

    current_ts = now_ts()
    for reminder in matches:
        uuid = reminder_uuid(reminder)
        if not uuid:
            fatal("Reminder is missing UUID")
        raw = reminders_mut(data)
        for index, raw_reminder in enumerate(raw):
            if reminder_uuid(raw_reminder) == uuid:
                raw.pop(index)
                break
        else:
            fatal("Reminder UUID not found during delete")
        set_tombstone(data, uuid, current_ts)
        print(f"Deleted: '{reminder_title(reminder)}'")
    write_db(data)
    run_best_effort("open", "-a", "Due")


def build_change_set(args: argparse.Namespace, reminder: dict[str, Any], index: int) -> ChangeSet:
    current_title = reminder_title(reminder)
    current_due_ts = reminder_due_ts(reminder)
    if current_due_ts is None:
        fatal("Reminder is missing due timestamp")
    new_title = args.title if args.title is not None else current_title
    new_due_ts = parse_time(args.rel, args.at, args.date, timezone=args.timezone) or current_due_ts
    changed: list[str] = []
    if new_title != current_title:
        changed.append(f"title → '{new_title}'")
    if new_due_ts != current_due_ts:
        changed.append(f"due → {fmt_ts(new_due_ts)}")
    if not changed:
        fatal("Nothing to change. Use --title, --in, --at, or --date.")
    return ChangeSet(
        title=new_title,
        due_ts=new_due_ts,
        changed=changed,
        recur=recur_label(reminder.get("rf") if isinstance(reminder.get("rf"), str) else None),
    )


def cmd_edit(args: argparse.Namespace) -> None:
    data = read_db()
    raw_idx, reminder = get_reminder(data, args.index)
    uuid = reminder_uuid(reminder)
    if not uuid:
        fatal("Reminder is missing UUID")
    changes = build_change_set(args, reminder, args.index)
    ensure_no_duplicates(changes.title, [changes.due_ts], data)

    reminders_mut(data).pop(raw_idx)
    set_tombstone(data, uuid, now_ts())
    write_db(data)

    ok = add_one(changes.title, changes.due_ts, changes.recur, args.autosnooze, args.timezone)
    if ok:
        print(f"Updated #{args.index}: {', '.join(changes.changed)} — synced to iPhone via CloudKit (AppleScript)")
        git_snapshot(read_db())
    else:
        print(
            f"Updated #{args.index}: {', '.join(changes.changed)} — Due editor open, please click Save manually to sync to iPhone."
        )


def cmd_log(args: argparse.Namespace) -> None:
    data = read_db()
    entries = data.get("lb")
    if not isinstance(entries, list):
        entries = []
    entries = sorted(entries, key=lambda entry: entry.get("m", 0), reverse=True)
    filter_lower = args.filter.lower() if args.filter else None
    shown: list[dict[str, Any]] = []
    for entry in entries:
        title = entry.get("n", "")
        if filter_lower and filter_lower not in str(title).lower():
            continue
        shown.append(entry)
        if len(shown) >= args.n:
            break
    if not shown:
        print("No completions found.")
        return

    print(f"{'Completed (HKT)':<20} Title")
    print("─" * 68)
    for entry in shown:
        ts = int(entry.get("m", 0))
        completed = hkt_from_ts(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"
        title = str(entry.get("n", ""))[:46]
        print(f"{completed:<20} {title}")


def cmd_snapshot(_: argparse.Namespace) -> None:
    data = read_db()
    if not isinstance(data, dict) or not data:
        log_line("ERROR: Could not read Due DB (permission error or DB unavailable).")
        raise SystemExit(1)

    path = snapshot_path()
    if path.exists():
        current = comparable_snapshot(data)
        try:
            last_json = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fatal(f"Failed to parse {path}: {exc}")
        last = [
            {
                "title": entry.get("title", ""),
                "due": entry.get("due_ts"),
                "recur": entry.get("recur"),
            }
            for entry in last_json
        ]
        if current == last:
            log_line("No changes since last snapshot.")
            return

    git_snapshot(data)
    log_line(f"Snapshot committed ({len(reminders_slice(data))} reminders).")


def cmd_search(args: argparse.Namespace) -> None:
    url = f"due:///search?query={quote(args.query)}"
    if args.section:
        url += f"&section={quote(args.section)}"
    run_best_effort("open", url)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moneo", description="Due app reminder manager")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("ls", help="List all reminders").set_defaults(func=cmd_ls)
    subparsers.add_parser("snapshot", help="Read DB and git-commit snapshot (no write)").set_defaults(func=cmd_snapshot)

    add = subparsers.add_parser("add", help="Add a reminder")
    add.add_argument("title")
    add.add_argument("--in", dest="rel", metavar="TIME")
    add.add_argument("--at", metavar="HH:MM")
    add.add_argument("--date", metavar="YYYY-MM-DD")
    add.add_argument(
        "--due",
        metavar="WHEN",
        help="Date and/or time: 'today 16:15', 'tomorrow 09:00', '2026-03-16 10:00', 'today', '16:15'",
    )
    add.add_argument("--recur", metavar="FREQ")
    add.add_argument("--every", metavar="INTERVAL")
    add.add_argument("--until", metavar="YYYY-MM-DD")
    add.add_argument("--no-skip-night", action="store_true")
    add.add_argument("--autosnooze", type=int)
    add.add_argument("--timezone", default="Asia/Hong_Kong")
    add.set_defaults(func=cmd_add)

    rm = subparsers.add_parser(
        "rm",
        aliases=["delete"],
        help="Delete a reminder by title (Mac only; does not sync to iPhone)",
    )
    rm.add_argument("pattern", metavar="PATTERN")
    rm.set_defaults(func=cmd_rm)

    edit = subparsers.add_parser("edit", help="Edit a reminder by index")
    edit.add_argument("index", type=int)
    edit.add_argument("--title")
    edit.add_argument("--in", dest="rel", metavar="TIME")
    edit.add_argument("--at", metavar="HH:MM")
    edit.add_argument("--date", metavar="YYYY-MM-DD")
    edit.add_argument("--autosnooze", type=int)
    edit.add_argument("--timezone", default="Asia/Hong_Kong")
    edit.set_defaults(func=cmd_edit)

    log = subparsers.add_parser("log", help="Show completion history from Due DB")
    log.add_argument("--n", "-n", type=int, default=20, help="Number of entries to show")
    log.add_argument("--filter", help="Filter by title substring (case-insensitive)")
    log.set_defaults(func=cmd_log)

    search = subparsers.add_parser("search", help="Launch Due and search")
    search.add_argument("query")
    search.add_argument("--section", choices=["Reminders", "Timers", "Logbook"])
    search.set_defaults(func=cmd_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        print()
        return 0
    try:
        args.func(args)
    except MoneoError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
