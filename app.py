import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import date, datetime, timedelta
from functools import wraps
from getpass import getpass
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, session, url_for
from health_import import normalize_health_payload
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
            "measurements": {"user_id": "INTEGER REFERENCES users(id)"},
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


def gym_sessions(db, user_id, since=None, limit=None):
    """Logged gym workouts grouped into sessions, newest first."""
    query = "SELECT * FROM workouts WHERE user_id = ?"
    params = [user_id]
    if since:
        query += " AND workout_date >= ?"
        params.append(since.isoformat())
    query += " ORDER BY workout_date DESC, id DESC"
    sessions = {}
    for row in db.execute(query, params):
        key = row["session_id"] or f'day:{row["workout_date"]}'
        session_entry = sessions.setdefault(key, {"kind": "gym", "date": row["workout_date"], "exercises": [], "sets": 0, "volume": 0.0})
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


def health_workouts(db, user_id, since=None, limit=None):
    query = "SELECT * FROM health_workouts WHERE user_id = ?"
    params = [user_id]
    if since:
        query += " AND workout_date >= ?"
        params.append(since.isoformat())
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
    db = get_db()
    user_id = g.user["id"]
    today = local_today()
    monday = today - timedelta(days=today.weekday())
    week_gym = gym_sessions(db, user_id, since=monday)
    week_health = health_workouts(db, user_id, since=monday)
    gym_days = {entry["date"] for entry in week_gym}
    health_days = {entry["date"] for entry in week_health}
    week = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        iso = day.isoformat()
        week.append({
            "label": day.strftime("%a")[0], "day": day.day, "is_today": day == today, "future": day > today,
            "gym": iso in gym_days, "health": iso in health_days,
        })

    last7 = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
    daily_rows = db.execute(
        "SELECT day, metric, value FROM health_daily WHERE user_id = ? AND day >= ?",
        (user_id, last7[0].isoformat()),
    ).fetchall()
    daily = {(row["day"], row["metric"]): row["value"] for row in daily_rows}
    health_week_kcal = health_workouts(db, user_id, since=last7[0])
    kcal_by_day = []
    for day in last7:
        iso = day.isoformat()
        value = daily.get((iso, "active_kcal"))
        if value is None:
            # Without daily totals, fall back to the workouts' own calories.
            value = sum(w["active_kcal"] or 0 for w in health_week_kcal if w["workout_date"] == iso) or None
        kcal_by_day.append({"label": day.strftime("%a")[0], "value": value, "is_today": day == today})
    peak = max((item["value"] or 0 for item in kcal_by_day), default=0)
    for item in kcal_by_day:
        item["height"] = round(100 * (item["value"] or 0) / peak) if peak else 0
    week_kcal = sum(item["value"] or 0 for day, item in zip(last7, kcal_by_day) if day >= monday)
    steps_today = daily.get((today.isoformat(), "steps"))

    timeline = gym_sessions(db, user_id, limit=6) + health_workouts(db, user_id, limit=6)
    timeline.sort(key=lambda entry: (entry["date"], entry.get("started_at") or ""), reverse=True)
    latest_weight, weight_change = body_weight_summary(db, user_id)
    return render_template(
        "overview.html", tab="home", today=today, week=week,
        week_days=len(gym_days | health_days), week_sets=sum(entry["sets"] for entry in week_gym),
        week_volume=sum(entry["volume"] for entry in week_gym), week_kcal=week_kcal,
        has_health=bool(daily_rows or health_week_kcal) or has_health_data(db, user_id),
        kcal_by_day=kcal_by_day, steps_today=steps_today,
        timeline=timeline[:6], exercises=recent_exercises(db, user_id),
        latest_weight=latest_weight, weight_change=weight_change,
    )


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
    return render_template("log.html", tab="log", today=local_today().isoformat(),
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
    return render_template(
        "progress.html", tab="progress", lift_charts=lift_progression(db, user_id),
        measurements=measurements, chart_points=chart_points, chart_min=chart_min, chart_max=chart_max,
        chart_polyline=polyline, latest_weight=latest_weight, weight_change=weight_change,
        activities=health_workouts(db, user_id, limit=20),
    )


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
    return render_template(
        "settings.html", tab="settings", new_api_key=new_api_key, has_key=has_key,
        health_url=url_for("api_import_health", _external=True), sync=sync,
    )


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
    user_id = g.api_user["id"]
    db = get_db()
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
    db.commit()
    return {
        "workouts_saved": len(imported["workouts"]),
        "days_saved": len({day for day, _metric in imported["daily"]}),
        "skipped": imported["skipped"],
    }


@app.errorhandler(413)
def payload_too_large(_error):
    if request.path.startswith("/api/"):
        api_log_failure(413, "body larger than the upload limit")
        return {"error": "This export is too large (over 50 MB). Export a shorter date range, then sync the rest in parts."}, 413
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


@app.get("/api/me")
@api_key_required
def api_me():
    return {"username": g.api_user["username"]}


@app.get("/openapi.yaml")
def openapi_spec():
    return send_file(Path(__file__).with_name("openapi.yaml"), mimetype="application/yaml")


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
    raise SystemExit("Usage: python app.py create-user <username> | list-users")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        cli()
    else:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
