import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from getpass import getpass
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from health_import import metric_group, normalize_health_payload
import push
from workout_parser import parse_workout_note
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(os.environ.get("DATABASE_PATH", "data/timgym.db"))
LOCAL_TZ = ZoneInfo(os.environ["TIMEZONE"]) if os.environ.get("TIMEZONE") else None
app = Flask(__name__)
# Behind the HTTPS reverse proxy, generate https:// links (e.g. the Apple Health URL).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    # Static files are versioned by URL (app.css?v=...), so phones can keep them.
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=30),
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # first Apple Health syncs can cover months of data
)


def load_secret_key():
    configured = os.environ.get("SECRET_KEY")
    if configured:
        return configured
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    key_file = DB_PATH.parent / ".session_secret"
    try:
        return key_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Prepare a complete candidate, then publish it without replacing a
        # key another Gunicorn worker may have created at the same time.
        key = secrets.token_urlsafe(48)
        candidate = DB_PATH.parent / f".session_secret.{secrets.token_hex(8)}"
        try:
            with candidate.open("x", encoding="utf-8") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                candidate.chmod(0o600)
            except OSError:
                pass
            try:
                os.link(candidate, key_file)
                return key
            except FileExistsError:
                return key_file.read_text(encoding="utf-8")
        finally:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


app.secret_key = load_secret_key()
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workout_date TEXT NOT NULL,
    exercise TEXT NOT NULL,
    sets INTEGER NOT NULL CHECK (sets > 0),
    reps INTEGER NOT NULL CHECK (reps > 0),
    weight_kg REAL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    measured_on TEXT NOT NULL,
    weight_kg REAL NOT NULL CHECK (weight_kg > 0),
    body_fat_pct REAL,
    waist_cm REAL,
    chest_cm REAL,
    hips_cm REAL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS health_workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    workout_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    activity TEXT NOT NULL,
    duration_s INTEGER,
    active_kcal REAL,
    distance_km REAL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, external_id)
);
CREATE INDEX IF NOT EXISTS health_workouts_by_date ON health_workouts (user_id, workout_date);
CREATE TABLE IF NOT EXISTS health_daily (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, day, metric)
);
CREATE TABLE IF NOT EXISTS health_sleep (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day TEXT NOT NULL,  -- the morning the night ended
    total_h REAL NOT NULL,
    deep_h REAL,
    core_h REAL,
    rem_h REAL,
    awake_h REAL,
    sleep_start TEXT,
    sleep_end TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS health_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    summary_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    activity TEXT NOT NULL,
    started_at TEXT NOT NULL,           -- UTC, ISO 8601
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished', 'discarded')),
    exercises_json TEXT NOT NULL DEFAULT '[]',
    watch_ended_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS live_sessions_by_user ON live_sessions (user_id, status);
CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS api_tokens (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize_db():
    db = connect_db()
    try:
        db.executescript(SCHEMA)
        for table, columns in {
            "workouts": {
                "user_id": "INTEGER REFERENCES users(id)",
                "session_id": "TEXT",
                "sets_json": "TEXT",
            },
            # source is 'apple_health' for imported check-ins, NULL for ones entered in TimGym.
            "measurements": {"user_id": "INTEGER REFERENCES users(id)", "source": "TEXT"},
        }.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for name, definition in columns.items():
                if name not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        db.execute("CREATE INDEX IF NOT EXISTS workouts_by_user_date ON workouts (user_id, workout_date)")
        db.commit()
    finally:
        db.close()


initialize_db()


def get_db():
    if "db" not in g:
        g.db = connect_db()
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def csrf_token():
    token = session.get("_csrf")
    if token is None:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def valid_csrf():
    expected = session.get("_csrf", "")
    actual = request.form.get("_csrf", "") or request.headers.get("X-CSRF-Token", "")
    return bool(expected and actual and secrets.compare_digest(expected, actual))


@app.before_request
def start_timer():
    g.started = time.perf_counter()


@app.after_request
def add_server_timing(response):
    # Lets the browser's network panel show how long TimGym itself took.
    if "started" in g:
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - g.started) * 1000:.1f}"
    return response


@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    g.user = get_db().execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def valid_date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a valid date.") from exc


def number(value, label, maximum, minimum=0):
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if not minimum <= result <= maximum:
        raise ValueError(f"{label} is outside the allowed range.")
    return result


def api_log_failure(status, reason):
    """Record why an API request was refused, for `docker compose logs`."""
    app.logger.warning(
        "API %s %s refused (%s): %s; content-type=%r, length=%s, user-agent=%r",
        request.method, request.path, status, reason, request.content_type,
        request.content_length, request.user_agent.string[:80],
    )


def api_key_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "").strip()
        # Accept "Bearer <key>" in any case, or the bare key, since apps vary in how they set headers.
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else auth
        if not token:
            api_log_failure(401, "no Authorization header")
            return {"error": "Provide your TimGym API key in the Authorization header as: Bearer YOUR_KEY"}, 401
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        user = get_db().execute(
            "SELECT users.id, users.username FROM api_tokens JOIN users ON users.id = api_tokens.user_id WHERE api_tokens.token_hash = ?",
            (digest,),
        ).fetchone()
        if user is None:
            api_log_failure(401, "unknown API key")
            return {"error": "Invalid API key. Generate a new one in TimGym Settings and update the app."}, 401
        g.api_user = user
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        if not valid_csrf():
            flash("Your sign-in session expired. Refresh the page and try again.")
            return redirect(url_for("login"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            flash("Signed in.")
            return redirect(url_for("index"))
        flash("Username or password is incorrect.")
    return render_template("login.html")


@app.post("/logout")
@login_required
def logout():
    if not valid_csrf():
        abort(400)
    session.clear()
    return redirect(url_for("login"))


def local_today():
    """Today in the configured TIMEZONE (the container clock is usually UTC)."""
    return datetime.now(LOCAL_TZ).date() if LOCAL_TZ else date.today()


def asset_version():
    try:
        return int((Path(app.static_folder) / "app.css").stat().st_mtime)
    except OSError:
        return 0


@app.context_processor
def template_globals():
    return {"user": g.get("user"), "csrf": csrf_token(), "asset_version": asset_version()}


@app.template_filter("relative_day")
def relative_day(value):
    day = date.fromisoformat(value) if isinstance(value, str) else value
    delta = (local_today() - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if 1 < delta < 7:
        return day.strftime("%A")
    return day.strftime("%a %d %b") if day.year == local_today().year else day.strftime("%d %b %Y")


@app.template_filter("kg")
def format_kg(value):
    return f"{value:g}" if value is not None else "—"


@app.template_filter("clock")
def clock(started_at):
    """"2026-10-21T18:05:00+03:00" -> "18:05" (the time as recorded on the phone)."""
    return str(started_at or "")[11:16]


@app.template_filter("duration")
def format_duration(seconds):
    if not seconds:
        return ""
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def row_sets(row):
    if row["sets_json"]:
        return json.loads(row["sets_json"])
    return [{"reps": row["reps"], "weight_kg": row["weight_kg"]} for _ in range(row["sets"])]


def summarize_sets(sets):
    """Compact gym notation: "3×5 @ 100 kg", "100 kg · 5, 5, 4", "60×8, 70×6, 80×5 kg"."""
    if not sets:
        return ""
    weights = {one.get("weight_kg") for one in sets}
    reps = [one["reps"] for one in sets]
    if len(weights) == 1:
        (weight,) = weights
        load = f" @ {weight:g} kg" if weight is not None else ""
        if len(set(reps)) == 1:
            return f"{len(sets)}×{reps[0]}{load}"
        return (f"{weight:g} kg · " if weight is not None else "") + ", ".join(str(r) for r in reps) + (" reps" if weight is None else "")
    return ", ".join(
        f'{one["weight_kg"]:g}×{one["reps"]}' if one.get("weight_kg") is not None else f'BW×{one["reps"]}'
        for one in sets
    ) + " kg"


def top_set(sets):
    """The heaviest set, ties broken by reps."""
    weighted = [one for one in sets if one.get("weight_kg") is not None]
    if not weighted:
        return None
    return max(weighted, key=lambda one: (one["weight_kg"], one["reps"]))


def gym_sessions(db, user_id, since=None, limit=None, until=None):
    """Logged gym workouts grouped into sessions, newest first."""
    query = "SELECT * FROM workouts WHERE user_id = ?"
    params = [user_id]
    if since:
        query += " AND workout_date >= ?"
        params.append(since.isoformat())
    if until:
        query += " AND workout_date <= ?"
        params.append(until.isoformat())
    query += " ORDER BY workout_date DESC, id DESC"
    sessions = {}
    for row in db.execute(query, params):
        key = row["session_id"] or f'day:{row["workout_date"]}'
        session_entry = sessions.setdefault(key, {"kind": "gym", "date": row["workout_date"], "exercises": [], "sets": 0,
                                                   "volume": 0.0, "logged_at": row["created_at"], "health": None})
        session_entry["logged_at"] = min(session_entry["logged_at"], row["created_at"])
        sets = row_sets(row)
        session_entry["exercises"].append({"name": row["exercise"], "summary": summarize_sets(sets), "notes": row["notes"]})
        session_entry["sets"] += len(sets)
        session_entry["volume"] += sum(one["reps"] * (one.get("weight_kg") or 0) for one in sets)
        if limit and len(sessions) > limit:
            sessions.pop(key)
            break
    for entry in sessions.values():
        entry["exercises"].reverse()  # in the order they were logged
    return list(sessions.values())


@app.template_test("strength")
def strength_test(name):
    return is_strength_activity(name)


STRENGTH_ACTIVITY_WORDS = ("strength", "core training", "cross training", "functional", "hiit", "high intensity")


def is_strength_activity(name):
    lowered = (name or "").casefold()
    return any(word in lowered for word in STRENGTH_ACTIVITY_WORDS)


def _aware(value, assume_tz):
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=assume_tz)


def link_health_to_gym(gym, health):
    """Pair each logged gym session with the Apple Health strength workout recorded for it.

    A gym session matches a strength-type workout on the same date. When a day has
    several, each log goes to the workout it was saved closest to (logs are usually
    saved during or soon after the workout). Returns the health workouts left over.
    """
    local = LOCAL_TZ or timezone.utc
    unmatched = list(health)
    for session_entry in sorted(gym, key=lambda entry: entry["logged_at"]):
        candidates = [w for w in unmatched if w["date"] == session_entry["date"] and is_strength_activity(w["activity"])]
        if not candidates:
            continue
        logged = _aware(session_entry["logged_at"].replace(" ", "T"), timezone.utc)

        def distance(workout):
            start = _aware(workout["started_at"], local)
            end = start + timedelta(seconds=workout["duration_s"] or 0)
            if start - timedelta(minutes=30) <= logged <= end + timedelta(hours=4):
                return timedelta(0), abs(logged - end)
            return abs(logged - end), timedelta(0)

        match = min(candidates, key=distance)
        session_entry["health"] = match
        match["linked_exercises"] = len(session_entry["exercises"])
        unmatched.remove(match)
    return unmatched


def health_workouts(db, user_id, since=None, limit=None, until=None):
    query = "SELECT * FROM health_workouts WHERE user_id = ?"
    params = [user_id]
    if since:
        query += " AND workout_date >= ?"
        params.append(since.isoformat())
    if until:
        query += " AND workout_date <= ?"
        params.append(until.isoformat())
    query += " ORDER BY started_at DESC"
    if limit:
        query += f" LIMIT {int(limit)}"
    return [dict(row, kind="health", date=row["workout_date"]) for row in db.execute(query, params)]


def recent_exercises(db, user_id, limit=8):
    """Exercises by most recent use, with last session, trend, and best."""
    rows = db.execute(
        "SELECT exercise, workout_date, sets, reps, weight_kg, sets_json FROM workouts WHERE user_id = ? ORDER BY workout_date DESC, id DESC",
        (user_id,),
    ).fetchall()
    by_name = {}
    for row in rows:
        key = row["exercise"].casefold()
        entry = by_name.setdefault(key, {"name": row["exercise"], "dates": [], "tops": {}, "best": None, "last_sets": None})
        sets = row_sets(row)
        if entry["last_sets"] is None:
            entry["last_sets"] = sets
            entry["last_date"] = row["workout_date"]
        best = top_set(sets)
        if best:
            current = entry["tops"].get(row["workout_date"])
            if current is None or (best["weight_kg"], best["reps"]) > (current["weight_kg"], current["reps"]):
                entry["tops"][row["workout_date"]] = best
            if entry["best"] is None or best["weight_kg"] > entry["best"]["weight_kg"]:
                entry["best"] = best
        if row["workout_date"] not in entry["dates"]:
            entry["dates"].append(row["workout_date"])
    results = []
    for entry in list(by_name.values())[:limit]:
        tops = [entry["tops"][day] for day in entry["dates"] if day in entry["tops"]]
        change = tops[0]["weight_kg"] - tops[1]["weight_kg"] if len(tops) > 1 else None
        results.append({
            "name": entry["name"], "last_date": entry["last_date"], "summary": summarize_sets(entry["last_sets"]),
            "top": top_set(entry["last_sets"]), "best": entry["best"], "change": change, "sessions": len(entry["dates"]),
        })
    return results


def body_weight_summary(db, user_id):
    rows = db.execute(
        "SELECT measured_on, weight_kg FROM measurements WHERE user_id = ? ORDER BY measured_on DESC, id DESC LIMIT 2",
        (user_id,),
    ).fetchall()
    latest = rows[0]["weight_kg"] if rows else None
    change = rows[0]["weight_kg"] - rows[1]["weight_kg"] if len(rows) > 1 else None
    return latest, change


@app.route("/")
@login_required
def index():
    """One day at a glance: workout, nutrition, and sleep, then body and recent lifts."""
    db = get_db()
    user_id = g.user["id"]
    today = local_today()
    try:
        day = min(date.fromisoformat(request.args.get("day", "")), today)
    except ValueError:
        day = today
    day_iso = day.isoformat()
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)

    # Week strip for the selected day's week; each day links to its own view.
    week_gym = gym_sessions(db, user_id, since=monday, until=sunday)
    week_health = health_workouts(db, user_id, since=monday, until=sunday)
    week_other = link_health_to_gym(week_gym, week_health)
    strength_days = {entry["date"] for entry in week_gym} | {
        entry["date"] for entry in week_other if is_strength_activity(entry["activity"])}
    other_days = {entry["date"] for entry in week_other if not is_strength_activity(entry["activity"])}
    week = []
    for offset in range(7):
        current = monday + timedelta(days=offset)
        iso = current.isoformat()
        week.append({
            "iso": iso, "label": current.strftime("%a")[0], "day": current.day,
            "is_today": current == today, "selected": current == day, "future": current > today,
            "gym": iso in strength_days, "health": iso in other_days,
        })
    week_minutes = round(sum(entry["duration_s"] or 0 for entry in week_health) / 60)

    # Workout for the day: gym logs (merged with their Apple Health session) and other activity.
    day_gym = [entry for entry in week_gym if entry["date"] == day_iso]
    day_other = [entry for entry in week_other if entry["date"] == day_iso]
    sessions = sorted(
        day_gym + day_other,
        key=lambda entry: entry.get("started_at") or (entry.get("health") or {}).get("started_at") or "",
    )

    # Daily totals for the day and the six before it (for the small charts).
    window = [day - timedelta(days=offset) for offset in range(6, -1, -1)]
    daily = {
        (row["day"], row["metric"]): row["value"]
        for row in db.execute(
            "SELECT day, metric, value FROM health_daily WHERE user_id = ? AND day BETWEEN ? AND ?",
            (user_id, window[0].isoformat(), day_iso),
        )
    }
    activity = {"active_kcal": daily.get((day_iso, "active_kcal")), "steps": daily.get((day_iso, "steps"))}
    if activity["active_kcal"] is None:
        # Without a daily total, the day's workouts give a lower bound for energy burned.
        activity["active_kcal"] = sum(w["active_kcal"] or 0 for w in week_health if w["date"] == day_iso) or None

    food = {metric: daily.get((day_iso, metric)) for metric in NUTRITION_METRICS}
    eaten_days = [daily[(current.isoformat(), "kcal_in")] for current in window if (current.isoformat(), "kcal_in") in daily]
    average_eaten = round(sum(eaten_days) / len(eaten_days)) if eaten_days else None
    macros = macro_breakdown(food)
    eaten = food["kcal_in"] or (macros["kcal"] if macros else None)
    has_nutrition = any(metric in NUTRITION_METRICS for _day, metric in daily) or db.execute(
        "SELECT EXISTS(SELECT 1 FROM health_daily WHERE user_id = ? AND metric = 'kcal_in')", (user_id,)
    ).fetchone()[0] == 1

    # Sleep: the night that ended on the selected morning, and the six before it.
    nights = {
        row["day"]: dict(row)
        for row in db.execute(
            "SELECT * FROM health_sleep WHERE user_id = ? AND day BETWEEN ? AND ?",
            (user_id, window[0].isoformat(), day_iso),
        )
    }
    night = nights.get(day_iso)
    if night:
        stages = [("deep", "Deep"), ("core", "Core"), ("rem", "REM"), ("awake", "Awake")]
        night["stages"] = [
            {"key": key, "label": label, "hours": night[f"{key}_h"]} for key, label in stages if night[f"{key}_h"]
        ]
        span = sum(stage["hours"] for stage in night["stages"]) or 1
        for stage in night["stages"]:
            stage["share"] = round(100 * stage["hours"] / span, 1)
    peak_sleep = max((n["total_h"] for n in nights.values()), default=0)
    sleep_week = [{
        "label": current.strftime("%a")[0], "selected": current == day,
        "hours": nights.get(current.isoformat(), {}).get("total_h"),
        "height": round(100 * nights[current.isoformat()]["total_h"] / max(peak_sleep, 8)) if current.isoformat() in nights else 0,
    } for current in window]
    has_sleep = bool(nights) or db.execute(
        "SELECT EXISTS(SELECT 1 FROM health_sleep WHERE user_id = ?)", (user_id,)
    ).fetchone()[0] == 1

    # Body as of the selected day.
    body_rows = db.execute(
        "SELECT measured_on, weight_kg, body_fat_pct, waist_cm, source FROM measurements WHERE user_id = ? AND measured_on <= ? ORDER BY measured_on DESC, id DESC LIMIT 60",
        (user_id, day_iso),
    ).fetchall()
    body = None
    if body_rows:
        latest = body_rows[0]
        previous = next((row for row in body_rows[1:] if row["measured_on"] != latest["measured_on"]), None)
        cutoff = (day - timedelta(days=30)).isoformat()
        body = {
            "date": latest["measured_on"], "weight": latest["weight_kg"], "source": latest["source"],
            "change": latest["weight_kg"] - previous["weight_kg"] if previous else None,
            "body_fat": next((row["body_fat_pct"] for row in body_rows if row["body_fat_pct"] is not None), None),
            "sparkline": sparkline([(row["measured_on"], row["weight_kg"]) for row in reversed(body_rows) if row["measured_on"] >= cutoff]),
        }

    live = active_live_session(db, user_id) if day == today else None
    return render_template(
        "overview.html", tab="home", today=today, day=day, week=week,
        live=live, live_sets=sum(len(e["sets"]) for e in live_exercises(live)) if live else 0,
        previous_day=(day - timedelta(days=1)).isoformat(), next_day=(day + timedelta(days=1)).isoformat() if day < today else None,
        week_days=len(strength_days | other_days), week_workout_seconds=week_minutes * 60,
        average_eaten=average_eaten,
        sessions=sessions, activity=activity,
        eaten=eaten, food=food, macros=macros, has_nutrition=has_nutrition,
        night=night, sleep_week=sleep_week, has_sleep=has_sleep,
        body=body, exercises=recent_exercises(db, user_id, limit=4),
    )


# Apple Health's workout names are long for a phone row.
SHORT_ACTIVITY_NAMES = {
    "traditional strength training": "Strength training",
    "functional strength training": "Functional strength",
    "high intensity interval training": "HIIT",
}


@app.template_filter("short_activity")
def short_activity(name):
    return SHORT_ACTIVITY_NAMES.get((name or "").casefold(), name)


@app.template_filter("hm")
def format_hm(value):
    """Compact duration from hours: 0.98 -> "59m", 3.34 -> "3h 20m"."""
    minutes = round((value or 0) * 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


@app.template_filter("hours")
def format_hours(value):
    """6.12 -> "6 h 07 min"."""
    if not value:
        return "—"
    minutes = round(value * 60)
    return f"{minutes // 60} h {minutes % 60:02d} min"


NUTRITION_METRICS = ("kcal_in", "protein_g", "carbs_g", "fat_g")


def macro_breakdown(values):
    """Grams, calories, and share of calories for protein, carbs, and fat."""
    per_gram = {"protein": 4, "carbs": 4, "fat": 9}
    grams = {name: values.get(f"{name}_g") for name in per_gram}
    kcal = {name: (grams[name] or 0) * factor for name, factor in per_gram.items()}
    total = sum(kcal.values())
    if not total:
        return None
    return {
        "kcal": total,
        "items": [
            {"key": name, "label": name.capitalize(), "grams": grams[name], "kcal": kcal[name], "share": round(100 * kcal[name] / total)}
            for name in per_gram
        ],
    }


def sparkline(points, width=300, height=56):
    """SVG polyline points for a small trend line; None with fewer than two readings."""
    if len(points) < 2:
        return None
    values = [value for _day, value in points]
    low, high = min(values), max(values)
    span = (high - low) or 1
    step = width / (len(points) - 1)
    return " ".join(f"{index * step:.1f},{height - 4 - (value - low) / span * (height - 8):.1f}" for index, value in enumerate(values))


def has_health_data(db, user_id):
    return db.execute(
        "SELECT EXISTS(SELECT 1 FROM health_workouts WHERE user_id = ?) OR EXISTS(SELECT 1 FROM health_daily WHERE user_id = ?)",
        (user_id, user_id),
    ).fetchone()[0] == 1


@app.get("/exercises/<path:name>")
@login_required
def exercise_history(name):
    rows = get_db().execute(
        "SELECT workout_date, exercise, sets, reps, weight_kg, sets_json, notes FROM workouts WHERE user_id = ? AND exercise = ? COLLATE NOCASE ORDER BY workout_date DESC, id DESC LIMIT 200",
        (g.user["id"], name),
    ).fetchall()
    if not rows:
        abort(404)
    entries = []
    best = None
    for row in rows:
        sets = row_sets(row)
        top = top_set(sets)
        if top and (best is None or top["weight_kg"] > best["weight_kg"]):
            best = top
        entries.append({"date": row["workout_date"], "summary": summarize_sets(sets), "top": top, "notes": row["notes"],
                        "volume": sum(one["reps"] * (one.get("weight_kg") or 0) for one in sets)})
    return render_template("exercise.html", tab="home", name=rows[0]["exercise"], entries=entries, best=best)


@app.get("/log")
@login_required
def log_page():
    # Opened from the "log your exercises" prompt for an Apple Health workout.
    health_session = None
    if request.args.get("session", "").isdigit():
        health_session = get_db().execute(
            "SELECT * FROM health_workouts WHERE id = ? AND user_id = ?", (int(request.args["session"]), g.user["id"])
        ).fetchone()
    return render_template("log.html", tab="log", today=local_today().isoformat(), health_session=health_session,
                           known_exercises=known_exercise_names(g.user["id"]))


@app.get("/progress")
@login_required
def progress():
    db = get_db()
    user_id = g.user["id"]
    measurements = db.execute(
        "SELECT * FROM measurements WHERE user_id = ? ORDER BY measured_on DESC, id DESC LIMIT 8",
        (user_id,),
    ).fetchall()
    history = db.execute(
        "SELECT measured_on, weight_kg FROM measurements WHERE user_id = ? ORDER BY measured_on, id",
        (user_id,),
    ).fetchall()
    chart_points = []
    if history:
        recent = history[-30:]
        weights = [row["weight_kg"] for row in recent]
        low, high = min(weights), max(weights)
        span = high - low or 1
        for i, row in enumerate(recent):
            x = 48 + (632 * i / max(len(recent) - 1, 1))
            y = 160 - (125 * (row["weight_kg"] - low) / span)
            chart_points.append({"x": round(x, 1), "y": round(y, 1), "weight": row["weight_kg"], "date": row["measured_on"]})
        chart_min, chart_max = round(low, 1), round(high, 1)
        polyline = " ".join(f'{point["x"]},{point["y"]}' for point in chart_points)
    else:
        chart_min = chart_max = None
        polyline = ""
    latest_weight, weight_change = body_weight_summary(db, user_id)
    activities = health_workouts(db, user_id, limit=20)
    sleep_chart = sleep_history(db, user_id, local_today(), days=30)
    if activities:
        link_health_to_gym(gym_sessions(db, user_id, since=date.fromisoformat(activities[-1]["date"])), activities)
    return render_template(
        "progress.html", tab="progress", lift_charts=lift_progression(db, user_id),
        measurements=measurements, chart_points=chart_points, chart_min=chart_min, chart_max=chart_max,
        chart_polyline=polyline, latest_weight=latest_weight, weight_change=weight_change,
        activities=activities, sleep_chart=sleep_chart,
    )


def sleep_history(db, user_id, until, days=30):
    """Nightly sleep for a stacked stage chart, with averages; None without data."""
    since = until - timedelta(days=days - 1)
    rows = {
        row["day"]: row
        for row in db.execute(
            "SELECT * FROM health_sleep WHERE user_id = ? AND day BETWEEN ? AND ?", (user_id, since.isoformat(), until.isoformat())
        )
    }
    if not rows:
        return None
    scale = max(9.0, max(row["total_h"] for row in rows.values()))
    nights = []
    for offset in range(days):
        current = since + timedelta(days=offset)
        row = rows.get(current.isoformat())
        segments = []
        if row:
            staged = sum(row[f"{key}_h"] or 0 for key in ("deep", "core", "rem"))
            parts = [(key, row[f"{key}_h"]) for key in ("deep", "core", "rem") if row[f"{key}_h"]] if staged else [("core", row["total_h"])]
            segments = [{"key": key, "height": round(100 * hours / scale, 1)} for key, hours in parts]
        nights.append({
            "date": current.isoformat(), "total": row["total_h"] if row else None, "segments": segments,
            # Label every 7th night counting back from the latest, so labels never collide.
            "label": current.strftime("%d") if (days - 1 - offset) % 7 == 0 else "",
        })
    def average(key):
        values = [row[key] for row in rows.values() if row[key] is not None]
        return sum(values) / len(values) if values else None
    return {
        "nights": nights, "count": len(rows), "scale": scale,
        "average_total": average("total_h"), "average_deep": average("deep_h"), "average_rem": average("rem_h"),
    }


def lift_progression(db, user_id):
    lift_defs = (("Squat", "squat"), ("Bench press", "bench"), ("Deadlift", "deadlift"))
    range_defs = (("1–5 reps", 1, 5, "1"), ("6–10 reps", 6, 10, "2"), ("11+ reps", 11, 999, "3"))
    raw_lift_rows = db.execute(
        "SELECT workout_date, exercise, sets, reps, weight_kg, sets_json FROM workouts WHERE user_id = ? ORDER BY workout_date, id",
        (user_id,),
    ).fetchall()
    lift_charts = []
    for lift_name, keyword in lift_defs:
        grouped = {key: {} for _label, _low, _high, key in range_defs}
        for row in raw_lift_rows:
            if keyword not in row["exercise"].casefold():
                continue
            for one in row_sets(row):
                reps = int(one.get("reps") or 0)
                weight = one.get("weight_kg")
                if weight is None or reps < 1:
                    continue
                weight = float(weight)
                for _label, low_reps, high_reps, key in range_defs:
                    if low_reps <= reps <= high_reps:
                        previous = grouped[key].get(row["workout_date"])
                        grouped[key][row["workout_date"]] = max(previous, weight) if previous is not None else weight
                        break
        all_dates = sorted({workout_date for values in grouped.values() for workout_date in values})
        all_weights = [weight for values in grouped.values() for weight in values.values()]
        has_data = bool(all_weights)
        if has_data:
            actual_low, actual_high = min(all_weights), max(all_weights)
            padding = max((actual_high - actual_low) * 0.12, 1.0)
            chart_low = max(0.0, actual_low - padding)
            chart_high = actual_high + padding
            date_positions = {workout_date: 48 + 632 * index / max(len(all_dates) - 1, 1) for index, workout_date in enumerate(all_dates)}
            if len(all_dates) == 1:
                date_positions[all_dates[0]] = 364
            series = []
            for label, _low, _high, key in range_defs:
                points = []
                for workout_date in sorted(grouped[key]):
                    weight = grouped[key][workout_date]
                    y = 154 - 112 * (weight - chart_low) / (chart_high - chart_low)
                    points.append({"x": round(date_positions[workout_date], 1), "y": round(y, 1), "weight": weight, "date": workout_date})
                series.append({
                    "key": key, "label": label, "points": points,
                    "polyline": " ".join(f'{point["x"]},{point["y"]}' for point in points),
                })
            first_date, last_date = all_dates[0], all_dates[-1]
        else:
            chart_low = chart_high = None
            series = []
            first_date = last_date = None
        lift_charts.append({
            "name": lift_name, "series": series, "has_data": has_data,
            "chart_min": chart_low, "chart_max": chart_high,
            "first_date": first_date, "last_date": last_date,
        })
    return lift_charts


def known_exercise_names(user_id):
    """Exercise names the user has logged, most recently used first."""
    rows = get_db().execute(
        "SELECT exercise FROM workouts WHERE user_id = ? GROUP BY exercise COLLATE NOCASE ORDER BY MAX(id) DESC LIMIT 200",
        (user_id,),
    ).fetchall()
    return [row["exercise"] for row in rows]


def render_settings(new_api_key=None):
    db = get_db()
    user_id = g.user["id"]
    sync = db.execute(
        """SELECT (SELECT COUNT(*) FROM health_workouts WHERE user_id = :u) AS workouts,
                  (SELECT COUNT(DISTINCT day) FROM health_daily WHERE user_id = :u) AS days,
                  MAX(COALESCE((SELECT MAX(updated_at) FROM health_workouts WHERE user_id = :u), ''),
                      COALESCE((SELECT MAX(updated_at) FROM health_daily WHERE user_id = :u), '')) AS last_sync""",
        {"u": user_id},
    ).fetchone()
    has_key = db.execute("SELECT 1 FROM api_tokens WHERE user_id = ?", (user_id,)).fetchone() is not None
    imports = []
    for row in db.execute("SELECT received_at, summary_json FROM health_imports WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user_id,)):
        item = dict(json.loads(row["summary_json"]), received_at=row["received_at"])
        groups = {metric_group(name) for name in item.get("metrics_received", [])}
        item["groups"] = [label for key, label in (("activity", "Activity"), ("nutrition", "Nutrition"), ("sleep", "Sleep"), ("body", "Body")) if key in groups]
        imports.append(item)
    push_devices = db.execute("SELECT COUNT(*) FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchone()[0]
    return render_template(
        "settings.html", tab="settings", new_api_key=new_api_key, has_key=has_key,
        push_devices=push_devices, vapid_public_key=push.public_key(vapid_keys()),
        live_start_url=url_for("api_live_start", _external=True), live_end_url=url_for("api_live_end", _external=True),
        health_url=url_for("api_import_health", _external=True), sync=sync, imports=imports,
    )


MANUAL_IMPORT_LIMIT = 100 * 1024 * 1024


@app.post("/settings/import")
@login_required
def import_health_file():
    # A full-history export is bigger than an automatic sync, so allow more here.
    request.max_content_length = MANUAL_IMPORT_LIMIT
    if not valid_csrf():
        abort(400)
    upload = request.files.get("export")
    if upload is None or not upload.filename:
        flash("Choose a Health Auto Export JSON file to import.")
        return redirect(url_for("settings"))
    try:
        payload = json.load(upload.stream)
    except (ValueError, UnicodeDecodeError):
        flash("That file is not valid JSON. In Health Auto Export, export with the JSON format.")
        return redirect(url_for("settings"))
    try:
        imported = normalize_health_payload(payload)
    except ValueError as exc:
        flash(f"Could not import that file: {exc}")
        return redirect(url_for("settings"))
    summary = store_health_import(get_db(), g.user["id"], imported, source=f"file {upload.filename[:60]}")
    parts = [
        f'{summary["workouts_saved"]} workouts', f'{summary["days_saved"]} days of activity and nutrition',
        f'{summary["nights_saved"]} nights of sleep', f'{summary["measurements_saved"]} body check-ins',
    ]
    flash("Imported " + ", ".join(parts) + (f' ({summary["skipped"]} entries skipped).' if summary["skipped"] else "."))
    return redirect(url_for("settings"))


@app.get("/settings")
@login_required
def settings():
    return render_settings()


@app.get("/connection")
def connection():
    return redirect(url_for("settings"), code=301)


@app.post("/measurements")
@login_required
def add_measurement():
    if not valid_csrf():
        abort(400)
    try:
        measured_on = valid_date(request.form.get("measured_on"))
        weight = number(request.form.get("weight_kg"), "Body weight", 1000, 0.1)
        body_fat = number(request.form.get("body_fat_pct"), "Body fat", 100)
        waist = number(request.form.get("waist_cm"), "Waist", 500)
        chest = number(request.form.get("chest_cm"), "Chest", 500)
        hips = number(request.form.get("hips_cm"), "Hips", 500)
        # A check-in entered here replaces the Apple Health reading for that day.
        get_db().execute(
            "DELETE FROM measurements WHERE user_id = ? AND measured_on = ? AND source = 'apple_health'",
            (g.user["id"], measured_on),
        )
        get_db().execute(
            "INSERT INTO measurements (user_id, measured_on, weight_kg, body_fat_pct, waist_cm, chest_cm, hips_cm) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (g.user["id"], measured_on, weight, body_fat, waist, chest, hips),
        )
        get_db().commit()
        flash("Check-in saved.")
        return redirect(url_for("progress"))
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("log_page") + "#check-in")


@app.post("/account/api-key")
@login_required
def create_api_key():
    if not valid_csrf():
        abort(400)
    token = "tg_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    db = get_db()
    db.execute(
        """INSERT INTO api_tokens (user_id, token_hash) VALUES (?, ?)
           ON CONFLICT(user_id) DO UPDATE SET token_hash = excluded.token_hash, created_at = CURRENT_TIMESTAMP""",
        (g.user["id"], digest),
    )
    db.commit()
    return render_settings(new_api_key=token)


@app.get("/manifest.webmanifest")
def web_manifest():
    manifest = {
        "name": "TimGym", "short_name": "TimGym", "start_url": "/", "scope": "/", "display": "standalone",
        "background_color": "#090d0b", "theme_color": "#090d0b",
        "icons": [
            {"src": url_for("static", filename="icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": url_for("static", filename="icons/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    }
    return app.response_class(json.dumps(manifest), mimetype="application/manifest+json")


@app.get("/favicon.ico")
def favicon():
    return send_file(Path(app.static_folder) / "icons" / "icon-192.png", mimetype="image/png")


def normalize_workout_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("Send a JSON object.")
    workout_date = valid_date(payload.get("date") or local_today().isoformat())
    exercises = payload.get("exercises")
    if not isinstance(exercises, list) or not 1 <= len(exercises) <= 30:
        raise ValueError("Include between 1 and 30 exercises.")
    normalized_exercises = []
    for exercise in exercises:
        if not isinstance(exercise, dict):
            raise ValueError("Each exercise must be an object.")
        name = str(exercise.get("name", "")).strip()
        sets = exercise.get("sets")
        if not name or len(name) > 80:
            raise ValueError("Each exercise needs a name of at most 80 characters.")
        if not isinstance(sets, list) or not 1 <= len(sets) <= 99:
            raise ValueError(f"Provide one or more sets for {name}.")
        normalized = []
        for one in sets:
            if not isinstance(one, dict):
                raise ValueError("Each set must include reps and may include weight_kg and rpe.")
            if one.get("reps") is None:
                raise ValueError(f"Add the reps for {name} before saving.")
            try:
                reps = int(one.get("reps", 0))
            except (ValueError, TypeError) as exc:
                raise ValueError("Reps must be a number.") from exc
            if not 1 <= reps <= 999:
                raise ValueError("Reps must be between 1 and 999.")
            weight = number(one.get("weight_kg"), "Weight", 2000)
            rpe = number(one.get("rpe"), "RPE", 10, 0)
            if rpe is not None and rpe < 1:
                raise ValueError("RPE must be between 1 and 10.")
            normalized.append({"reps": reps, "weight_kg": weight, "rpe": rpe})
        normalized_exercises.append({
            "name": name,
            "notes": str(exercise.get("notes", "")).strip()[:500],
            "sets": normalized,
        })
    return workout_date, normalized_exercises


def persist_workout(workout_date, exercises, user_id):
    session_id = str(uuid.uuid4())
    rows = []
    for exercise in exercises:
        first = exercise["sets"][0]
        rows.append((
            user_id, session_id, workout_date, exercise["name"], len(exercise["sets"]),
            first["reps"], first["weight_kg"], exercise["notes"], json.dumps(exercise["sets"]),
        ))
    db = get_db()
    db.executemany(
        """INSERT INTO workouts
           (user_id, session_id, workout_date, exercise, sets, reps, weight_kg, notes, sets_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    db.commit()
    return {"workout_id": session_id, "date": workout_date, "exercises_saved": len(rows)}


@app.post("/workouts/interpret")
@login_required
def interpret_workout():
    if not valid_csrf():
        return {"error": "Your session expired. Refresh the page and try again."}, 400
    payload = request.get_json(silent=True) or {}
    note = str(payload.get("note", "")).strip()
    if not note:
        return {"error": "Write a workout first."}, 400
    parsed = parse_workout_note(note, known_exercises=known_exercise_names(g.user["id"]), today=local_today())
    if "clarification" in parsed:
        return {"clarification": parsed["clarification"]}
    draft = parsed["workout"]
    return {
        "workout": {
            "date": draft["date"],
            "exercises": [
                {
                    "name": exercise["name"],
                    "notes": exercise["notes"],
                    "default_weight_kg": exercise["default_weight_kg"],
                    "sets": exercise["sets"],
                    "missing_fields": exercise["missing_fields"],
                }
                for exercise in draft["exercises"]
            ],
        }
    }


@app.post("/workouts/save")
@login_required
def save_web_workout():
    if not valid_csrf():
        return {"error": "Your session expired. Refresh the page and try again."}, 400
    payload = request.get_json(silent=True)
    try:
        workout_date, exercises = normalize_workout_payload(payload)
        result = persist_workout(workout_date, exercises, g.user["id"])
        return result, 201
    except ValueError as exc:
        return {"error": str(exc)}, 400


@app.get("/api/workouts")
@api_key_required
def api_list_workouts():
    try:
        limit = max(1, min(int(request.args.get("limit", "20")), 100))
    except ValueError:
        return {"error": "limit must be an integer from 1 to 100."}, 400
    rows = get_db().execute(
        "SELECT workout_date, exercise, sets, reps, weight_kg, notes, sets_json FROM workouts WHERE user_id = ? ORDER BY workout_date DESC, id DESC LIMIT ?",
        (g.api_user["id"], limit),
    ).fetchall()
    results = []
    for row in rows:
        result = {
            "date": row["workout_date"], "exercise": row["exercise"],
            "sets": json.loads(row["sets_json"]) if row["sets_json"] else [
                {"reps": row["reps"], "weight_kg": row["weight_kg"]} for _ in range(row["sets"])
            ],
            "notes": row["notes"],
        }
        results.append(result)
    return {"workouts": results}


@app.post("/api/workouts")
@api_key_required
def api_add_workout():
    payload = request.get_json(silent=True)
    try:
        workout_date, exercises = normalize_workout_payload(payload)
        return persist_workout(workout_date, exercises, g.api_user["id"]), 201
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}, 400


def save_health_measurements(db, user_id, body):
    """Store one Apple Health check-in per day, updated in place on re-sync.
    A check-in entered in TimGym for the same day takes precedence."""
    saved = 0
    for day, values in sorted(body.items()):
        rows = db.execute(
            "SELECT id, source, weight_kg FROM measurements WHERE user_id = ? AND measured_on = ?", (user_id, day)
        ).fetchall()
        if any(row["source"] != "apple_health" for row in rows):
            continue
        existing = rows[0] if rows else None
        weight = values.get("weight_kg", existing["weight_kg"] if existing else None)
        if weight is None:
            continue  # a check-in needs a body weight
        if existing:
            db.execute(
                """UPDATE measurements SET weight_kg = ?, body_fat_pct = COALESCE(?, body_fat_pct),
                   waist_cm = COALESCE(?, waist_cm) WHERE id = ?""",
                (weight, values.get("body_fat_pct"), values.get("waist_cm"), existing["id"]),
            )
        else:
            db.execute(
                "INSERT INTO measurements (user_id, measured_on, weight_kg, body_fat_pct, waist_cm, source) VALUES (?, ?, ?, ?, ?, 'apple_health')",
                (user_id, day, weight, values.get("body_fat_pct"), values.get("waist_cm")),
            )
        saved += 1
    return saved


def store_health_import(db, user_id, imported, source):
    """Save normalized Apple Health data (from the sync API or a file upload) and
    record a summary for Settings. Re-imports update rows in place."""
    db.executemany(
        """INSERT INTO health_workouts
           (user_id, external_id, workout_date, started_at, activity, duration_s, active_kcal, distance_km)
           VALUES (:user_id, :external_id, :workout_date, :started_at, :activity, :duration_s, :active_kcal, :distance_km)
           ON CONFLICT (user_id, external_id) DO UPDATE SET
             workout_date = excluded.workout_date, started_at = excluded.started_at, activity = excluded.activity,
             duration_s = excluded.duration_s, active_kcal = excluded.active_kcal, distance_km = excluded.distance_km,
             updated_at = CURRENT_TIMESTAMP""",
        [dict(workout, user_id=user_id) for workout in imported["workouts"]],
    )
    # Each export carries the day's running total, so the latest value replaces the stored one.
    db.executemany(
        """INSERT INTO health_daily (user_id, day, metric, value) VALUES (?, ?, ?, ?)
           ON CONFLICT (user_id, day, metric) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP""",
        [(user_id, day, metric, value) for (day, metric), value in imported["daily"].items()],
    )
    body_saved = save_health_measurements(db, user_id, imported["body"])
    # Each export carries whole nights, so a re-sent night replaces the stored one.
    db.executemany(
        """INSERT INTO health_sleep (user_id, day, total_h, deep_h, core_h, rem_h, awake_h, sleep_start, sleep_end)
           VALUES (:user_id, :day, :total_h, :deep_h, :core_h, :rem_h, :awake_h, :start, :end)
           ON CONFLICT (user_id, day) DO UPDATE SET total_h = excluded.total_h, deep_h = excluded.deep_h,
             core_h = excluded.core_h, rem_h = excluded.rem_h, awake_h = excluded.awake_h,
             sleep_start = excluded.sleep_start, sleep_end = excluded.sleep_end, updated_at = CURRENT_TIMESTAMP""",
        [dict(night, user_id=user_id) for night in imported["sleep"].values()],
    )
    summary = {
        "source": source,
        "workouts_received": imported["received"]["workouts"],
        "measurements_saved": body_saved,
        "nights_saved": len(imported["sleep"]),
        "workouts_saved": len(imported["workouts"]),
        "days_saved": len({day for day, _metric in imported["daily"]}),
        "metrics_received": imported["received"]["metrics"],
        "metrics_unused": imported["unused_metrics"],
        "skipped": imported["skipped"],
        "skipped_reasons": imported["skipped_reasons"],
    }
    # Keep a short history so Settings can show what the last syncs contained.
    db.execute("INSERT INTO health_imports (user_id, summary_json) VALUES (?, ?)", (user_id, json.dumps(summary)))
    db.execute(
        "DELETE FROM health_imports WHERE user_id = ? AND id NOT IN (SELECT id FROM health_imports WHERE user_id = ? ORDER BY id DESC LIMIT 20)",
        (user_id, user_id),
    )
    db.commit()
    if imported["skipped_reasons"]:
        app.logger.warning("Apple Health import for user %s skipped %s entries: %s", user_id, imported["skipped"], "; ".join(imported["skipped_reasons"]))
    return summary




@app.post("/api/health")
@api_key_required
def api_import_health():
    try:
        # Some exporters omit the JSON content type, so parse the body regardless.
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            raise ValueError("The body is not valid JSON. Choose the JSON export format.")
        imported = normalize_health_payload(payload)
    except ValueError as exc:
        api_log_failure(400, str(exc))
        return {"error": str(exc)}, 400
    return store_health_import(get_db(), g.api_user["id"], imported, source="sync")


@app.errorhandler(413)
def payload_too_large(_error):
    if request.path.startswith("/api/"):
        api_log_failure(413, "body larger than the upload limit")
        return {"error": "This export is too large (over 50 MB). Export a shorter date range, then sync the rest in parts."}, 413
    if request.path == "/settings/import":
        flash("That file is over 100 MB. Export a shorter date range and import it in parts.")
        return redirect(url_for("settings"))
    return "Upload too large.", 413


@app.errorhandler(405)
def method_not_allowed(_error):
    if request.path.startswith("/api/"):
        return {"error": f"{request.method} is not supported here. Send a POST request with JSON."}, 405
    return "Method not allowed.", 405


@app.errorhandler(500)
def server_error(_error):
    if request.path.startswith("/api/"):
        return {"error": "TimGym hit an unexpected error while saving. Check the server logs."}, 500
    return "Something went wrong.", 500


# ---------------------------------------------------------------------------
# Live workouts: a Shortcut (Apple Watch workout starts) or the Log page opens a
# session; the phone gets a push notification that opens /live/<id>, where sets
# are added as you go and saved as a normal workout at the end.

LIVE_REUSE_WINDOW = timedelta(hours=4)


def vapid_keys():
    if "vapid" not in g:
        g.vapid = push.load_or_create_vapid(DB_PATH.parent)
    return g.vapid


def notify(user_id, payload):
    """Send a push to every device the user enabled; forget ones that are gone."""
    db = get_db()
    subscriptions = db.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?", (user_id,)).fetchall()
    if not subscriptions:
        return 0
    delivered, gone = push.send(vapid_keys(), [dict(row) for row in subscriptions], payload, request.host_url.rstrip("/"))
    if gone:
        db.executemany("DELETE FROM push_subscriptions WHERE endpoint = ?", [(endpoint,) for endpoint in gone])
        db.commit()
    return delivered


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def active_live_session(db, user_id):
    """The user's live session started within the reuse window, if any."""
    cutoff = (utc_now() - LIVE_REUSE_WINDOW).isoformat()
    return db.execute(
        "SELECT * FROM live_sessions WHERE user_id = ? AND status = 'active' AND started_at >= ? ORDER BY id DESC LIMIT 1",
        (user_id, cutoff),
    ).fetchone()


def start_live_session(db, user_id, activity):
    """Return (session, created). An active session is reused so a Shortcut firing
    twice, or a manual start during a watch workout, doesn't split the log."""
    existing = active_live_session(db, user_id)
    if existing:
        return existing, False
    cursor = db.execute(
        "INSERT INTO live_sessions (user_id, activity, started_at) VALUES (?, ?, ?)",
        (user_id, activity, utc_now().isoformat()),
    )
    db.commit()
    return db.execute("SELECT * FROM live_sessions WHERE id = ?", (cursor.lastrowid,)).fetchone(), True


def owned_live_session(session_id):
    row = get_db().execute(
        "SELECT * FROM live_sessions WHERE id = ? AND user_id = ?", (session_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def live_exercises(row):
    return json.loads(row["exercises_json"])


def save_live_exercises(session_id, exercises):
    db = get_db()
    db.execute("UPDATE live_sessions SET exercises_json = ? WHERE id = ?", (json.dumps(exercises), session_id))
    db.commit()


def last_sets_by_exercise(user_id):
    """Each exercise's last logged set, to prefill the steppers."""
    last = {}
    for row in get_db().execute(
        "SELECT exercise, sets_json, reps, weight_kg, sets FROM workouts WHERE user_id = ? ORDER BY workout_date DESC, id DESC LIMIT 300",
        (user_id,),
    ):
        key = row["exercise"].casefold()
        if key not in last:
            sets = row_sets(row)
            if sets:
                last[key] = {"reps": sets[-1]["reps"], "weight_kg": sets[-1].get("weight_kg")}
    return last


@app.post("/api/live/start")
@api_key_required
def api_live_start():
    payload = request.get_json(force=True, silent=True) or {}
    activity = str(payload.get("activity") or "Strength training").strip()[:80] or "Strength training"
    db = get_db()
    session_row, created = start_live_session(db, g.api_user["id"], activity)
    url = url_for("live_page", session_id=session_row["id"], _external=True)
    delivered = 0
    if created:
        delivered = notify(g.api_user["id"], {
            "title": f"{activity} started",
            "body": "Tap to log your sets as you go.",
            "url": url_for("live_page", session_id=session_row["id"]),
            "tag": f"live-{session_row['id']}",
        })
    return {"session_id": session_row["id"], "url": url, "created": created, "notified_devices": delivered}, 201 if created else 200


@app.post("/api/live/end")
@api_key_required
def api_live_end():
    db = get_db()
    session_row = active_live_session(db, g.api_user["id"])
    if session_row is None:
        return {"session_id": None, "message": "No live workout is open."}
    db.execute("UPDATE live_sessions SET watch_ended_at = ? WHERE id = ?", (utc_now().isoformat(), session_row["id"]))
    db.commit()
    count = sum(len(exercise["sets"]) for exercise in live_exercises(session_row))
    delivered = notify(g.api_user["id"], {
        "title": "Workout finished on your watch",
        "body": f"Save your {count} logged set{'' if count == 1 else 's'}." if count else "Add your sets and save the workout.",
        "url": url_for("live_page", session_id=session_row["id"]),
        "tag": f"live-{session_row['id']}",
    })
    return {"session_id": session_row["id"], "sets_logged": count, "notified_devices": delivered}


@app.post("/live/start")
@login_required
def live_start():
    if not valid_csrf():
        abort(400)
    session_row, _created = start_live_session(get_db(), g.user["id"], "Strength training")
    return redirect(url_for("live_page", session_id=session_row["id"]))


@app.get("/live")
@login_required
def live_current():
    session_row = active_live_session(get_db(), g.user["id"])
    return redirect(url_for("live_page", session_id=session_row["id"]) if session_row else url_for("log_page"))


@app.get("/live/<int:session_id>")
@login_required
def live_page(session_id):
    session_row = owned_live_session(session_id)
    if session_row["status"] != "active":
        flash("That workout is already " + ("saved." if session_row["status"] == "finished" else "discarded."))
        return redirect(url_for("index"))
    started = datetime.fromisoformat(session_row["started_at"])
    return render_template(
        "live.html", tab="log", live=session_row, exercises=live_exercises(session_row),
        started_local=started.astimezone(LOCAL_TZ) if LOCAL_TZ else started,
        known_exercises=known_exercise_names(g.user["id"])[:20], last_sets=last_sets_by_exercise(g.user["id"]),
    )


@app.post("/live/<int:session_id>/sets")
@login_required
def live_add_set(session_id):
    if not valid_csrf():
        return {"error": "Your session expired. Refresh the page and try again."}, 400
    session_row = owned_live_session(session_id)
    if session_row["status"] != "active":
        return {"error": "This workout is no longer open."}, 409
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("exercise", "")).strip()[:80]
    try:
        reps = int(payload.get("reps"))
        weight = number(payload.get("weight_kg"), "Weight", 2000)
    except (TypeError, ValueError):
        return {"error": "Enter reps as a whole number and weight in kg."}, 400
    if not name or not 1 <= reps <= 999:
        return {"error": "Choose an exercise and enter 1–999 reps."}, 400
    exercises = live_exercises(session_row)
    if len(exercises) >= 30 and not any(e["name"].casefold() == name.casefold() for e in exercises):
        return {"error": "A workout can hold up to 30 exercises."}, 400
    # Sets for an exercise done earlier in the session join that exercise's list.
    target = next((e for e in exercises if e["name"].casefold() == name.casefold()), None)
    if target is None:
        target = {"name": name, "sets": []}
        exercises.append(target)
    if len(target["sets"]) >= 99:
        return {"error": "That exercise already has 99 sets."}, 400
    target["sets"].append({"reps": reps, "weight_kg": weight})
    # The most recent exercise moves last, so "undo" and the list follow the order you train.
    exercises.remove(target)
    exercises.append(target)
    save_live_exercises(session_id, exercises)
    return {"exercises": exercises}


@app.post("/live/<int:session_id>/undo")
@login_required
def live_undo(session_id):
    if not valid_csrf():
        return {"error": "Your session expired. Refresh the page and try again."}, 400
    session_row = owned_live_session(session_id)
    exercises = live_exercises(session_row)
    if exercises:
        exercises[-1]["sets"].pop()
        if not exercises[-1]["sets"]:
            exercises.pop()
        save_live_exercises(session_id, exercises)
    return {"exercises": exercises}


@app.post("/live/<int:session_id>/finish")
@login_required
def live_finish(session_id):
    if not valid_csrf():
        abort(400)
    session_row = owned_live_session(session_id)
    if session_row["status"] != "active":
        return redirect(url_for("index"))
    db = get_db()
    exercises = live_exercises(session_row)
    started = datetime.fromisoformat(session_row["started_at"])
    workout_day = (started.astimezone(LOCAL_TZ) if LOCAL_TZ else started).date()
    if exercises:
        workout_date, normalized = normalize_workout_payload({"date": workout_day.isoformat(), "exercises": exercises})
        persist_workout(workout_date, normalized, g.user["id"])
        flash(f"Workout saved: {len(normalized)} exercise{'' if len(normalized) == 1 else 's'}, "
              f"{sum(len(e['sets']) for e in normalized)} sets.")
    db.execute(
        "UPDATE live_sessions SET status = ?, finished_at = ? WHERE id = ?",
        ("finished" if exercises else "discarded", utc_now().isoformat(), session_id),
    )
    db.commit()
    return redirect(url_for("index") if workout_day == local_today() else url_for("index", day=workout_day.isoformat()))


@app.post("/live/<int:session_id>/discard")
@login_required
def live_discard(session_id):
    if not valid_csrf():
        abort(400)
    owned_live_session(session_id)
    db = get_db()
    db.execute("UPDATE live_sessions SET status = 'discarded', finished_at = ? WHERE id = ?", (utc_now().isoformat(), session_id))
    db.commit()
    flash("Live workout discarded.")
    return redirect(url_for("index"))


@app.post("/push/subscribe")
@login_required
def push_subscribe():
    if not valid_csrf():
        return {"error": "Your session expired. Refresh the page and try again."}, 400
    payload = request.get_json(silent=True) or {}
    endpoint = str(payload.get("endpoint", ""))
    keys = payload.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        return {"error": "That subscription is incomplete."}, 400
    db = get_db()
    db.execute(
        """INSERT INTO push_subscriptions (endpoint, user_id, p256dh, auth) VALUES (?, ?, ?, ?)
           ON CONFLICT (endpoint) DO UPDATE SET user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth""",
        (endpoint[:1000], g.user["id"], str(keys["p256dh"])[:200], str(keys["auth"])[:100]),
    )
    db.commit()
    return {"ok": True}


@app.post("/push/test")
@login_required
def push_test():
    if not valid_csrf():
        return {"error": "Your session expired. Refresh the page and try again."}, 400
    delivered = notify(g.user["id"], {"title": "TimGym", "body": "Notifications are working.", "url": url_for("index")})
    if not delivered:
        return {"error": "No device received it. Enable notifications on this device first."}, 404
    return {"delivered": delivered}


@app.get("/sw.js")
def service_worker():
    # Served from the root so it can handle notifications for every page.
    response = send_file(Path(app.static_folder) / "sw.js", mimetype="text/javascript", max_age=0)
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@app.get("/api/me")
@api_key_required
def api_me():
    return {"username": g.api_user["username"]}


@app.get("/openapi.yaml")
def openapi_spec():
    return send_file(Path(__file__).with_name("openapi.yaml"), mimetype="application/yaml", max_age=0)


def create_user(username, password):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        raise ValueError("Username must be 3-32 characters: letters, numbers, dot, dash, underscore.")
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters.")
    db = connect_db()
    try:
        if db.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone():
            raise ValueError("That username already exists.")
        is_first = db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        user_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
        if is_first:
            # Existing pre-login records belong to the first account created.
            db.execute("UPDATE workouts SET user_id = ? WHERE user_id IS NULL", (user_id,))
            db.execute("UPDATE measurements SET user_id = ? WHERE user_id IS NULL", (user_id,))
        db.commit()
    finally:
        db.close()


def manual_data_counts(db, user_id):
    """What delete-manual-data would remove: everything not imported from Apple Health."""
    return {
        "gym exercises logged": db.execute("SELECT COUNT(*) FROM workouts WHERE user_id = ?", (user_id,)).fetchone()[0],
        "body check-ins entered by hand": db.execute(
            "SELECT COUNT(*) FROM measurements WHERE user_id = ? AND source IS NOT 'apple_health'", (user_id,)
        ).fetchone()[0],
    }


def delete_manual_data(username):
    """Delete a user's gym logs and hand-entered check-ins; keep Apple Health data,
    the account, and the API key. Returns the counts removed."""
    db = connect_db()
    try:
        user = db.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user is None:
            raise ValueError(f"No account named '{username}'.")
        counts = manual_data_counts(db, user["id"])
        db.execute("DELETE FROM workouts WHERE user_id = ?", (user["id"],))
        db.execute("DELETE FROM measurements WHERE user_id = ? AND source IS NOT 'apple_health'", (user["id"],))
        db.commit()
        return counts
    finally:
        db.close()


def cli():
    if len(sys.argv) == 3 and sys.argv[1] == "create-user":
        username = sys.argv[2].strip()
        password = getpass("New password (minimum 12 characters): ")
        confirmation = getpass("Confirm password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match.")
        create_user(username, password)
        print(f"Created account '{username}'.")
        return
    if len(sys.argv) == 2 and sys.argv[1] == "list-users":
        db = connect_db()
        for row in db.execute("SELECT username, created_at FROM users ORDER BY username"):
            print(f'{row["username"]}\t{row["created_at"]}')
        db.close()
        return
    if len(sys.argv) == 3 and sys.argv[1] == "delete-manual-data":
        username = sys.argv[2].strip()
        db = connect_db()
        user = db.execute("SELECT id, username FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user is None:
            db.close()
            raise SystemExit(f"No account named '{username}'.")
        counts = manual_data_counts(db, user["id"])
        db.close()
        print(f"This permanently deletes for '{user['username']}':")
        for label, count in counts.items():
            print(f"  {count} {label}")
        print("Apple Health workouts, activity, nutrition, imported check-ins, the account, and the API key are kept.")
        if not any(counts.values()):
            print("Nothing to delete.")
            return
        if input(f"Type the username '{user['username']}' to confirm: ").strip() != user["username"]:
            raise SystemExit("Cancelled; nothing was deleted.")
        delete_manual_data(user["username"])
        print("Deleted.")
        return
    raise SystemExit("Usage: python app.py create-user <username> | list-users | delete-manual-data <username>")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
