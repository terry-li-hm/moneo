use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio, exit};
use std::thread::sleep;
use std::time::Duration;

use chrono::{
    DateTime, Datelike, Duration as ChronoDuration, LocalResult, NaiveDate, NaiveTime, TimeZone,
    Timelike, Utc,
};
use chrono_tz::Asia::Hong_Kong;
use clap::{CommandFactory, Parser, Subcommand};
use flate2::Compression;
use flate2::read::GzDecoder;
use flate2::write::GzEncoder;
use serde::Serialize;
use serde_json::{Map, Value, json};

const APPLE_SCRIPT: &str = r#"
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
    "#;

const RECUR_CHOICES: [&str; 5] = ["daily", "weekly", "monthly", "quarterly", "yearly"];

#[derive(Parser)]
#[command(name = "moneo", about = "Due app reminder manager")]
struct Cli {
    #[command(subcommand)]
    cmd: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    #[command(about = "List all reminders")]
    Ls,
    #[command(about = "Read DB and git-commit snapshot (no write)")]
    Snapshot,
    #[command(about = "Add a reminder")]
    Add {
        title: String,
        #[arg(long = "in", value_name = "TIME")]
        rel: Option<String>,
        #[arg(long, value_name = "HH:MM")]
        at: Option<String>,
        #[arg(long, value_name = "YYYY-MM-DD")]
        date: Option<String>,
        #[arg(long, value_name = "FREQ", value_parser = RECUR_CHOICES)]
        recur: Option<String>,
    },
    #[command(about = "Delete a reminder by title (Mac only; does not sync to iPhone)")]
    Rm {
        #[arg(long, value_name = "PATTERN")]
        title: String,
    },
    #[command(about = "Edit a reminder by index")]
    Edit {
        index: usize,
        #[arg(long)]
        title: Option<String>,
        #[arg(long = "in", value_name = "TIME")]
        rel: Option<String>,
        #[arg(long, value_name = "HH:MM")]
        at: Option<String>,
        #[arg(long, value_name = "YYYY-MM-DD")]
        date: Option<String>,
    },
    #[command(about = "Show completion history from Due DB")]
    Log {
        #[arg(long, short, default_value = "20", help = "Number of entries to show")]
        n: usize,
        #[arg(long, help = "Filter by title substring (case-insensitive)")]
        filter: Option<String>,
    },
}

#[derive(Serialize)]
struct SnapshotReminder {
    title: String,
    due: Option<String>,
    due_ts: Option<i64>,
    recur: Option<String>,
    uuid: Option<String>,
}

#[derive(Serialize)]
struct ComparableSnapshotReminder {
    title: String,
    due: Option<i64>,
    recur: Option<String>,
}

fn cmd_log(n: usize, filter: Option<String>) {
    let data = read_db();
    let mut entries: Vec<_> = data
        .get("lb")
        .and_then(Value::as_array)
        .map(|arr| arr.iter().collect())
        .unwrap_or_default();

    // Sort by completion time descending
    entries.sort_by(|a, b| {
        b.get("m").and_then(Value::as_i64).unwrap_or(0)
            .cmp(&a.get("m").and_then(Value::as_i64).unwrap_or(0))
    });

    // Apply filter
    let filter_lower = filter.as_deref().map(str::to_lowercase);
    let entries: Vec<_> = entries
        .into_iter()
        .filter(|r| {
            if let Some(ref f) = filter_lower {
                r.get("n").and_then(Value::as_str).unwrap_or("").to_lowercase().contains(f.as_str())
            } else {
                true
            }
        })
        .take(n)
        .collect();

    if entries.is_empty() {
        println!("No completions found.");
        return;
    }

    println!("{:<20} {}", "Completed (HKT)", "Title");
    println!("{}", "─".repeat(68));
    for r in entries {
        let ts = r.get("m").and_then(Value::as_i64).unwrap_or(0);
        let dt = Utc.timestamp_opt(ts, 0).single()
            .map(|t| t.with_timezone(&Hong_Kong).format("%Y-%m-%d %H:%M").to_string())
            .unwrap_or_else(|| "—".to_string());
        let title = r.get("n").and_then(Value::as_str).unwrap_or("").chars().take(46).collect::<String>();
        println!("{:<20} {}", dt, title);
    }
}

fn main() {
    let cli = Cli::parse();

    match cli.cmd {
        Some(Commands::Ls) => cmd_ls(),
        Some(Commands::Snapshot) => cmd_snapshot(),
        Some(Commands::Add {
            title,
            rel,
            at,
            date,
            recur,
        }) => cmd_add(title, rel, at, date, recur),
        Some(Commands::Rm { title }) => cmd_rm(title),
        Some(Commands::Edit {
            index,
            title,
            rel,
            at,
            date,
        }) => cmd_edit(index, title, rel, at, date),
        Some(Commands::Log { n, filter }) => cmd_log(n, filter),
        None => {
            let _ = Cli::command().print_help();
            println!();
        }
    }
}

fn fatal(message: impl AsRef<str>) -> ! {
    eprintln!("{}", message.as_ref());
    exit(1);
}

fn home_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| fatal("HOME is not set")))
}

fn due_db_path() -> PathBuf {
    home_dir().join("Library/Containers/com.phocusllp.duemac/Data/Library/Application Support/Due App/Due.duedb")
}

fn snapshot_path() -> PathBuf {
    home_dir().join("officina/backups/due-reminders.json")
}

fn log_path() -> PathBuf {
    home_dir().join("tmp/due-snapshot.log")
}

fn now_ts() -> i64 {
    Utc::now().timestamp()
}

fn hkt_from_ts(ts: i64) -> DateTime<chrono_tz::Tz> {
    Hong_Kong
        .timestamp_opt(ts, 0)
        .single()
        .unwrap_or_else(|| fatal(format!("Invalid timestamp: {ts}")))
}

fn hkt_now() -> DateTime<chrono_tz::Tz> {
    Utc::now().with_timezone(&Hong_Kong)
}

fn resolve_hkt(local: chrono::NaiveDateTime) -> DateTime<chrono_tz::Tz> {
    match Hong_Kong.from_local_datetime(&local) {
        LocalResult::Single(dt) => dt,
        LocalResult::Ambiguous(dt, _) => dt,
        LocalResult::None => fatal("Could not resolve local time in Asia/Hong_Kong"),
    }
}

fn read_db() -> Value {
    let path = due_db_path();
    let file = match File::open(&path) {
        Ok(file) => file,
        Err(err) if err.kind() == std::io::ErrorKind::PermissionDenied => return json!({}),
        Err(err) => fatal(format!("Failed to read {}: {err}", path.display())),
    };

    let decoder = GzDecoder::new(BufReader::new(file));
    serde_json::from_reader(decoder)
        .unwrap_or_else(|err| fatal(format!("Failed to parse {}: {err}", path.display())))
}

fn due_pid() -> Option<String> {
    let output = Command::new("pgrep")
        .args(["-x", "Due"])
        .output()
        .unwrap_or_else(|err| fatal(format!("Failed to run pgrep: {err}")));

    if output.status.success() {
        let pid = String::from_utf8_lossy(&output.stdout).trim().to_string();
        if pid.is_empty() { None } else { Some(pid) }
    } else {
        None
    }
}

fn run_best_effort(program: &str, args: &[&str]) {
    let _ = Command::new(program)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn write_db(data: &Value) {
    let due_db = due_db_path();
    let backup = due_db.with_extension("duedb.bak");

    fs::copy(&due_db, &backup).unwrap_or_else(|err| {
        fatal(format!(
            "Failed to back up {} to {}: {err}",
            due_db.display(),
            backup.display()
        ))
    });

    let pid = due_pid();
    if let Some(ref pid) = pid {
        run_best_effort("kill", &["-15", pid]);
        for _ in 0..20 {
            sleep(Duration::from_millis(200));
            if due_pid().is_none() {
                break;
            }
        }
    }

    let write_result = (|| -> Result<(), String> {
        let file = File::create(&due_db).map_err(|err| err.to_string())?;
        let writer = BufWriter::new(file);
        let mut encoder = GzEncoder::new(writer, Compression::default());
        serde_json::to_writer(&mut encoder, data).map_err(|err| err.to_string())?;
        let mut writer = encoder.finish().map_err(|err| err.to_string())?;
        writer.flush().map_err(|err| err.to_string())?;
        Ok(())
    })();

    if let Err(err) = write_result {
        fs::copy(&backup, &due_db).unwrap_or_else(|restore_err| {
            fatal(format!(
                "Write failed, and restore also failed for {}: {restore_err}",
                due_db.display()
            ))
        });
        fatal(format!("Write failed, restored backup: {err}"));
    }

    if pid.is_some() {
        run_best_effort("open", &["-a", "Due"]);
    }

    git_snapshot(data);
}

fn reminders_slice(data: &Value) -> &[Value] {
    data.get("re")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or(&[])
}

fn reminders_mut(data: &mut Value) -> &mut Vec<Value> {
    let root = data
        .as_object_mut()
        .unwrap_or_else(|| fatal("Due DB root is not a JSON object"));
    if !root.contains_key("re") {
        root.insert("re".to_string(), Value::Array(Vec::new()));
    }
    root.get_mut("re")
        .and_then(Value::as_array_mut)
        .unwrap_or_else(|| fatal("Due DB field 're' is not an array"))
}

fn sorted_reminders(data: &Value) -> Vec<Value> {
    let mut reminders = reminders_slice(data).to_vec();
    reminders.sort_by_key(|r| reminder_due_ts(r).unwrap_or(0));
    reminders
}

fn reminder_due_ts(reminder: &Value) -> Option<i64> {
    reminder.get("d").and_then(Value::as_i64)
}

fn reminder_title(reminder: &Value) -> String {
    reminder
        .get("n")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_string()
}

fn reminder_uuid(reminder: &Value) -> Option<String> {
    reminder
        .get("u")
        .and_then(Value::as_str)
        .map(ToString::to_string)
}

fn fmt_ts(ts: i64) -> String {
    let dt = hkt_from_ts(ts);
    let now = hkt_now();
    if dt.date_naive() == now.date_naive() {
        return dt.format("today %H:%M").to_string();
    }
    if dt.date_naive() == (now + ChronoDuration::days(1)).date_naive() {
        return dt.format("tomorrow %H:%M").to_string();
    }
    dt.format("%b %d %H:%M").to_string()
}

fn get_reminder(data: &Value, index: usize) -> (usize, Value) {
    let sorted = sorted_reminders(data);
    let idx = index
        .checked_sub(1)
        .unwrap_or_else(|| fatal(format!("Error: no reminder at index {index}.")));
    if idx >= sorted.len() {
        fatal(format!("Error: no reminder at index {index}."));
    }
    let target = sorted[idx].clone();
    let target_uuid = reminder_uuid(&target).unwrap_or_else(|| fatal("Reminder is missing UUID"));
    let raw = reminders_slice(data);
    let raw_idx = raw
        .iter()
        .position(|r| reminder_uuid(r).as_deref() == Some(target_uuid.as_str()))
        .unwrap_or_else(|| fatal("Reminder UUID not found in raw reminder list"));
    (raw_idx, target)
}

fn recur_label(code: Option<&str>) -> Option<String> {
    match code.unwrap_or("") {
        "d" => Some("daily".to_string()),
        "w" => Some("weekly".to_string()),
        "m" => Some("monthly".to_string()),
        "q" => Some("quarterly".to_string()),
        "y" => Some("yearly".to_string()),
        _ => None,
    }
}

fn recur_unit(freq: &str) -> Option<i32> {
    match freq {
        "daily" => Some(16),
        "weekly" => Some(256),
        "monthly" => Some(8),
        "quarterly" => Some(8),
        "yearly" => Some(4),
        _ => None,
    }
}

fn recur_freq(freq: &str) -> Option<i32> {
    match freq {
        "quarterly" => Some(3),
        "daily" | "weekly" | "monthly" | "yearly" => Some(1),
        _ => None,
    }
}

fn parse_time(rel: Option<&str>, at: Option<&str>, date: Option<&str>) -> Option<i64> {
    let now = hkt_now();

    if let Some(rel) = rel {
        if rel.len() < 2 {
            fatal(format!(
                "Error: invalid --in '{rel}'. Use e.g. 30m, 2h, 90s."
            ));
        }
        let (num, unit) = rel.split_at(rel.len() - 1);
        let n: i64 = num.parse().unwrap_or_else(|_| {
            fatal(format!(
                "Error: invalid --in '{rel}'. Use e.g. 30m, 2h, 90s."
            ))
        });
        let delta = match unit {
            "s" => ChronoDuration::seconds(n),
            "m" => ChronoDuration::minutes(n),
            "h" => ChronoDuration::hours(n),
            _ => fatal(format!(
                "Error: invalid --in '{rel}'. Use e.g. 30m, 2h, 90s."
            )),
        };
        return Some((now + delta).timestamp());
    }

    let mut base = now;
    if let Some(date) = date {
        let parsed = NaiveDate::parse_from_str(date, "%Y-%m-%d")
            .unwrap_or_else(|_| fatal(format!("Error: invalid --date '{date}'. Use YYYY-MM-DD.")));
        let naive = parsed
            .and_hms_opt(0, 0, 0)
            .unwrap_or_else(|| fatal("Invalid date"));
        base = resolve_hkt(naive);
    }

    if let Some(at) = at {
        let parsed = NaiveTime::parse_from_str(at, "%H:%M")
            .or_else(|_| NaiveTime::parse_from_str(at, "%-H:%M"))
            .unwrap_or_else(|_| fatal(format!("Error: invalid --at '{at}'. Use HH:MM.")));
        let h = parsed.hour();
        let m = parsed.minute();
        if h > 23 || m > 59 {
            fatal(format!(
                "Error: invalid --at '{at}'. Hour must be 0-23, minute 0-59."
            ));
        }
        let naive = base.date_naive().and_hms_opt(h, m, 0).unwrap_or_else(|| {
            fatal(format!(
                "Error: invalid --at '{at}'. Hour must be 0-23, minute 0-59."
            ))
        });
        return Some(resolve_hkt(naive).timestamp());
    }

    if date.is_some() {
        let naive = base
            .date_naive()
            .and_hms_opt(9, 0, 0)
            .unwrap_or_else(|| fatal("Invalid 09:00 timestamp"));
        return Some(resolve_hkt(naive).timestamp());
    }

    None
}

fn sync_via_applescript(title: &str, due_ts: i64, recur: Option<&str>) -> bool {
    let mut url = format!(
        "due://x-callback-url/add?title={}&duedate={}",
        urlencoding::encode(title),
        due_ts
    );

    if let Some(recur) = recur {
        if let (Some(unit), Some(freq)) = (recur_unit(recur), recur_freq(recur)) {
            url.push_str(&format!(
                "&recurunit={unit}&recurfreq={freq}&recurfromdate={due_ts}"
            ));
            if recur == "weekly" {
                let dt = hkt_from_ts(due_ts);
                let day = ((dt.weekday().num_days_from_monday() as i32) + 2) % 7;
                let day = if day == 0 { 7 } else { day };
                url.push_str(&format!("&recurbyday={day}"));
            }
        }
    }

    run_best_effort("caffeinate", &["-u", "-t", "1"]);
    sleep(Duration::from_millis(500));
    run_best_effort("open", &[&url]);
    sleep(Duration::from_secs(3));

    let output = Command::new("osascript")
        .args(["-e", APPLE_SCRIPT])
        .output()
        .unwrap_or_else(|err| fatal(format!("Failed to run osascript: {err}")));

    String::from_utf8_lossy(&output.stdout).contains("ok")
}

fn find_duplicate(title: &str, due_ts: i64) -> Option<Value> {
    let due_dt = hkt_from_ts(due_ts);
    let normalized = title.trim().to_lowercase();
    for reminder in reminders_slice(&read_db()) {
        if reminder_title(reminder).trim().to_lowercase() == normalized {
            if let Some(existing_due) = reminder_due_ts(reminder) {
                let existing_dt = hkt_from_ts(existing_due);
                if existing_dt.date_naive() == due_dt.date_naive()
                    && existing_dt.hour() == due_dt.hour()
                    && existing_dt.minute() == due_dt.minute()
                {
                    return Some(reminder.clone());
                }
            }
        }
    }
    None
}

fn make_snapshot(data: &Value) -> Vec<SnapshotReminder> {
    sorted_reminders(data)
        .into_iter()
        .map(|r| SnapshotReminder {
            title: reminder_title(&r),
            due: reminder_due_ts(&r).map(fmt_ts),
            due_ts: reminder_due_ts(&r),
            recur: recur_label(r.get("rf").and_then(Value::as_str)),
            uuid: reminder_uuid(&r),
        })
        .collect()
}

fn git_snapshot(data: &Value) {
    let snapshot = make_snapshot(data);
    let path = snapshot_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .unwrap_or_else(|err| fatal(format!("Failed to create {}: {err}", parent.display())));
    }

    let text = serde_json::to_string_pretty(&snapshot)
        .unwrap_or_else(|err| fatal(format!("Failed to serialize snapshot: {err}")));
    fs::write(&path, format!("{text}\n"))
        .unwrap_or_else(|err| fatal(format!("Failed to write {}: {err}", path.display())));

    let repo = path
        .parent()
        .unwrap_or_else(|| fatal("Snapshot path has no parent"));
    let _ = Command::new("git")
        .args([
            "-C",
            repo.to_str()
                .unwrap_or_else(|| fatal("Non-UTF8 snapshot repo path")),
            "add",
            path.to_str()
                .unwrap_or_else(|| fatal("Non-UTF8 snapshot path")),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    let _ = Command::new("git")
        .args([
            "-C",
            repo.to_str()
                .unwrap_or_else(|| fatal("Non-UTF8 snapshot repo path")),
            "commit",
            "-m",
            &format!("due: snapshot ({} reminders)", snapshot.len()),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

fn log_line(message: &str) {
    let ts = hkt_now().format("%Y-%m-%d %H:%M:%S").to_string();
    let line = format!("[{ts}] {message}\n");
    let path = log_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .unwrap_or_else(|err| fatal(format!("Failed to create {}: {err}", parent.display())));
    }
    let mut file = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .unwrap_or_else(|err| fatal(format!("Failed to open {}: {err}", path.display())));
    file.write_all(line.as_bytes())
        .unwrap_or_else(|err| fatal(format!("Failed to write {}: {err}", path.display())));
    print!("{line}");
    let _ = std::io::stdout().flush();
}

fn cmd_ls() {
    let data = read_db();
    let reminders = sorted_reminders(&data);
    if reminders.is_empty() {
        println!("No reminders.");
        return;
    }
    let now = now_ts();
    println!("{:<4} {:<36} {:<16} Recur", "#", "Title", "Due");
    println!("{}", "─".repeat(68));
    for (i, reminder) in reminders.iter().enumerate() {
        let title = reminder_title(reminder)
            .chars()
            .take(35)
            .collect::<String>();
        let due = reminder_due_ts(reminder).unwrap_or(0);
        let due_str = if due == 0 {
            "—".to_string()
        } else {
            fmt_ts(due)
        };
        let flag = if due != 0 && due < now { " ⚠" } else { "" };
        let recur = recur_label(reminder.get("rf").and_then(Value::as_str)).unwrap_or_default();
        println!(
            "{:<4} {:<36} {:<16} {}",
            i + 1,
            title,
            format!("{due_str}{flag}"),
            recur
        );
    }
}

fn cmd_add(
    title: String,
    rel: Option<String>,
    at: Option<String>,
    date: Option<String>,
    recur: Option<String>,
) {
    let due_ts = parse_time(rel.as_deref(), at.as_deref(), date.as_deref())
        .unwrap_or_else(|| fatal("Error: specify a time with --in, --at, or --date."));

    if let Some(dup) = find_duplicate(&title, due_ts) {
        let existing_due = reminder_due_ts(&dup).unwrap_or(0);
        fatal(format!(
            "Duplicate: '{}' already exists on that day at {}. Use 'moneo edit' to change the time.",
            title,
            fmt_ts(existing_due)
        ));
    }

    let ok = sync_via_applescript(&title, due_ts, recur.as_deref());
    let recur_str = recur
        .as_ref()
        .map(|r| format!(" (repeats {r})"))
        .unwrap_or_default();
    if ok {
        println!(
            "Added: '{}' due {}{} — synced to iPhone via CloudKit (AppleScript)",
            title,
            fmt_ts(due_ts),
            recur_str
        );
        git_snapshot(&read_db());
    } else {
        println!("Due editor open — please click Save manually to sync to iPhone.");
    }
}

fn set_tombstone(data: &mut Value, uuid: &str, ts: i64) {
    let root = data
        .as_object_mut()
        .unwrap_or_else(|| fatal("Due DB root is not a JSON object"));
    if !root.contains_key("dl") {
        root.insert("dl".to_string(), Value::Object(Map::new()));
    }
    let dl = root
        .get_mut("dl")
        .and_then(Value::as_object_mut)
        .unwrap_or_else(|| fatal("Due DB field 'dl' is not an object"));
    dl.insert(uuid.to_string(), Value::Number(ts.into()));
}

fn cmd_rm(title_pattern: String) {
    let mut data = read_db();
    let pattern = title_pattern.trim().to_lowercase();
    let matches: Vec<Value> = reminders_slice(&data)
        .iter()
        .filter(|r| reminder_title(r).to_lowercase().contains(&pattern))
        .cloned()
        .collect();

    if matches.is_empty() {
        fatal(format!("No reminders matching '{}'.", title_pattern));
    }

    let now = now_ts();
    for reminder in &matches {
        let uuid = reminder_uuid(reminder).unwrap_or_else(|| fatal("Reminder is missing UUID"));
        let title = reminder_title(reminder);
        let raw = reminders_mut(&mut data);
        let pos = raw
            .iter()
            .position(|r| reminder_uuid(r).as_deref() == Some(uuid.as_str()))
            .unwrap_or_else(|| fatal("Reminder UUID not found during delete"));
        raw.remove(pos);
        set_tombstone(&mut data, &uuid, now);
        println!("Deleted: '{}'", title);
    }
    write_db(&data);
    run_best_effort("open", &["-a", "Due"]);
}

fn cmd_edit(
    index: usize,
    title: Option<String>,
    rel: Option<String>,
    at: Option<String>,
    date: Option<String>,
) {
    let mut data = read_db();
    let (raw_idx, reminder) = get_reminder(&data, index);
    let current_title = reminder
        .get("n")
        .and_then(Value::as_str)
        .unwrap_or_else(|| fatal("Reminder is missing title"))
        .to_string();
    let current_due_ts = reminder
        .get("d")
        .and_then(Value::as_i64)
        .unwrap_or_else(|| fatal("Reminder is missing due timestamp"));
    let recur = recur_label(reminder.get("rf").and_then(Value::as_str));
    let uuid = reminder_uuid(&reminder).unwrap_or_else(|| fatal("Reminder is missing UUID"));

    let new_title = title.unwrap_or_else(|| current_title.clone());
    let new_due_ts =
        parse_time(rel.as_deref(), at.as_deref(), date.as_deref()).unwrap_or(current_due_ts);
    let mut changed = Vec::new();

    if new_title != current_title {
        changed.push(format!("title → '{}'", new_title));
    }

    if new_due_ts != current_due_ts {
        changed.push(format!("due → {}", fmt_ts(new_due_ts)));
    }

    if changed.is_empty() {
        fatal("Nothing to change. Use --title, --in, --at, or --date.");
    }

    reminders_mut(&mut data).remove(raw_idx);
    set_tombstone(&mut data, &uuid, now_ts());
    write_db(&data);

    let ok = sync_via_applescript(&new_title, new_due_ts, recur.as_deref());
    if ok {
        println!(
            "Updated #{}: {} — synced to iPhone via CloudKit (AppleScript)",
            index,
            changed.join(", ")
        );
        git_snapshot(&read_db());
    } else {
        println!(
            "Updated #{}: {} — Due editor open, please click Save manually to sync to iPhone.",
            index,
            changed.join(", ")
        );
    }
}

fn cmd_snapshot() {
    let data = read_db();
    if !data.is_object() || data.as_object().is_some_and(|obj| obj.is_empty()) {
        log_line("ERROR: Could not read Due DB (permission error or DB unavailable).");
        exit(1);
    }

    let path = snapshot_path();
    if path.exists() {
        let current: Vec<ComparableSnapshotReminder> = sorted_reminders(&data)
            .into_iter()
            .map(|r| ComparableSnapshotReminder {
                title: reminder_title(&r),
                due: reminder_due_ts(&r),
                recur: recur_label(r.get("rf").and_then(Value::as_str)),
            })
            .collect();

        let last_text = fs::read_to_string(&path)
            .unwrap_or_else(|err| fatal(format!("Failed to read {}: {err}", path.display())));
        let last_json: Vec<Value> = serde_json::from_str(&last_text)
            .unwrap_or_else(|err| fatal(format!("Failed to parse {}: {err}", path.display())));
        let last_comparable: Vec<ComparableSnapshotReminder> = last_json
            .into_iter()
            .map(|r| ComparableSnapshotReminder {
                title: r
                    .get("title")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                due: r.get("due_ts").and_then(Value::as_i64),
                recur: r
                    .get("recur")
                    .and_then(Value::as_str)
                    .map(ToString::to_string),
            })
            .collect();

        let current_value = serde_json::to_value(&current)
            .unwrap_or_else(|err| fatal(format!("Failed to serialize snapshot comparison: {err}")));
        let last_value = serde_json::to_value(&last_comparable)
            .unwrap_or_else(|err| fatal(format!("Failed to serialize snapshot comparison: {err}")));
        if current_value == last_value {
            log_line("No changes since last snapshot.");
            return;
        }
    }

    git_snapshot(&data);
    log_line(&format!(
        "Snapshot committed ({} reminders).",
        reminders_slice(&data).len()
    ));
}
