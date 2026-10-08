import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import uuid
from datetime import date, timedelta
from functools import wraps
from getpass import getpass
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template_string, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(os.environ.get("DATABASE_PATH", "data/timgym.db"))
app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=True,
    PERMANENT_SESSION_LIFETIME=timedelta(days=14),
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
    actual = request.form.get("_csrf", "")
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


def api_key_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return {"error": "Provide your TimGym API key as a Bearer token."}, 401
        digest = hashlib.sha256(auth[7:].strip().encode("utf-8")).hexdigest()
        user = get_db().execute(
            "SELECT users.id, users.username FROM api_tokens JOIN users ON users.id = api_tokens.user_id WHERE api_tokens.token_hash = ?",
            (digest,),
        ).fetchone()
        if user is None:
            return {"error": "Invalid API key."}, 401
        g.api_user = user
        return view(*args, **kwargs)
    return wrapped


BASE_STYLE = """
<style>
:root{color-scheme:light;--ink:#17231f;--muted:#62716a;--line:#dce5df;--green:#176b4b;--pale:#edf5ef}
*{box-sizing:border-box}body{margin:0;background:#f5f7f4;color:var(--ink);font:16px/1.45 system-ui,sans-serif}
header{background:#102b20;color:white;padding:20px max(18px,calc((100vw - 980px)/2));display:flex;justify-content:space-between;align-items:center;gap:14px}
header h1{margin:0;font-size:1.5rem}header p{margin:3px 0 0;color:#c7d8cd}.nav{display:flex;align-items:center;gap:12px}.nav a{color:#fff}
main{max-width:980px;padding:22px 18px 48px;margin:auto}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;align-items:start}
.card{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:0 2px 8px #122b2010}.wide{grid-column:1/-1}
h2{font-size:1.12rem;margin:0 0 15px}label{display:block;color:var(--muted);font-size:.85rem;margin:0 0 5px}
input{width:100%;min-height:42px;border:1px solid #cbd7cf;border-radius:8px;padding:9px 10px;font:inherit;color:var(--ink);background:white}
input:focus{outline:2px solid #a8d4ba;border-color:var(--green)}.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:12px}
.fields.three{grid-template-columns:repeat(3,minmax(0,1fr))}button{border:0;border-radius:8px;padding:11px 16px;color:white;background:var(--green);font:600 1rem system-ui;cursor:pointer}
button:hover{background:#105438}.muted{color:var(--muted);font-size:.9rem}.flash{padding:10px 12px;border-radius:8px;background:var(--pale);margin:0 0 14px}
.record{padding:12px 0;border-top:1px solid var(--line)}.record:first-of-type{border-top:0;padding-top:0}.record strong{display:block}.record small{color:var(--muted)}
.chart{width:100%;height:auto;min-height:160px}.chart text{fill:var(--muted);font:12px system-ui}.empty{padding:12px;color:var(--muted);background:#f6f8f6;border-radius:8px}
code,pre{overflow-wrap:anywhere}pre{white-space:pre-wrap;background:#f6f8f6;padding:12px;border-radius:8px}
@media(max-width:700px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}.fields.three{grid-template-columns:repeat(2,minmax(0,1fr))}header{align-items:flex-start;flex-direction:column}}
</style>
"""

LOGIN_PAGE = """
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign in · TimGym</title>""" + BASE_STYLE + """</head>
<body><header><div><h1>TimGym</h1><p>Sign in to your private training log</p></div></header><main>
{% for message in get_flashed_messages() %}<p class="flash">{{ message }}</p>{% endfor %}
<article class="card" style="max-width:440px;margin:30px auto"><h2>Sign in</h2>
<form method="post" action="{{ url_for('login') }}"><input type="hidden" name="_csrf" value="{{ csrf }}">
<div style="margin-bottom:12px"><label for="username">Username</label><input id="username" name="username" autocomplete="username" required></div>
<div style="margin-bottom:14px"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required></div>
<button type="submit">Sign in</button></form><p class="muted">Accounts are created by the TimGym owner.</p></article></main></body></html>
"""

DASHBOARD = """
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TimGym</title>""" + BASE_STYLE + """</head>
<body><header><div><h1>TimGym</h1><p>Personal training log</p></div><nav class="nav"><span>{{ user.username }}</span><form method="post" action="{{ url_for('logout') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button type="submit">Sign out</button></form></nav></header>
<main>{% for message in get_flashed_messages() %}<p class="flash">{{ message }}</p>{% endfor %}
{% if new_api_key %}<article class="card wide" style="margin-bottom:18px"><h2>Your new ChatGPT API key</h2><p>Copy this now. It will not be shown again. Keep it private.</p><pre>{{ new_api_key }}</pre></article>{% endif %}
<section class="grid">
<article class="card"><h2>Record measurements</h2><form method="post" action="{{ url_for('add_measurement') }}"><input type="hidden" name="_csrf" value="{{ csrf }}">
<div class="fields"><div><label for="measured_on">Date</label><input id="measured_on" name="measured_on" type="date" value="{{ today }}" required></div>
<div><label for="weight">Body weight (kg)</label><input id="weight" name="weight_kg" type="number" min="1" max="1000" step="0.1" required></div></div>
<div class="fields three"><div><label for="body_fat">Body fat (%)</label><input id="body_fat" name="body_fat_pct" type="number" min="0" max="100" step="0.1"></div>
<div><label for="waist">Waist (cm)</label><input id="waist" name="waist_cm" type="number" min="1" max="500" step="0.1"></div>
<div><label for="chest">Chest (cm)</label><input id="chest" name="chest_cm" type="number" min="1" max="500" step="0.1"></div></div>
<div style="margin-bottom:12px"><label for="hips">Hips (cm)</label><input id="hips" name="hips_cm" type="number" min="1" max="500" step="0.1"></div>
<button type="submit">Save measurements</button></form></article>
<article class="card"><h2>Workout logging</h2><p>Log workouts by telling ChatGPT what you did. Workout entry is not available on this website.</p>
<p class="muted">To connect ChatGPT, create your personal API key below, then add the TimGym Action using the OpenAPI file in the project README.</p>
<form method="post" action="{{ url_for('create_api_key') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button type="submit">Create or rotate ChatGPT key</button></form>
<p class="muted"><a href="{{ url_for('openapi_spec') }}">Download the ChatGPT Action OpenAPI file</a></p></article>
<article class="card wide"><h2>Body-weight trend</h2>
{% if chart_points %}<svg class="chart" viewBox="0 0 700 210" role="img" aria-label="Body weight trend chart">
<line x1="48" y1="170" x2="680" y2="170" stroke="#dce5df"/><line x1="48" y1="25" x2="48" y2="170" stroke="#dce5df"/>
<text x="4" y="30">{{ chart_max }} kg</text><text x="4" y="170">{{ chart_min }} kg</text>
<polyline fill="none" stroke="#176b4b" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="{{ chart_polyline }}"/>
{% for point in chart_points %}<circle cx="{{ point.x }}" cy="{{ point.y }}" r="4" fill="#176b4b"><title>{{ point.date }}: {{ point.weight }} kg</title></circle>{% endfor %}
<text x="48" y="198">{{ chart_points[0].date }}</text><text x="620" y="198">{{ chart_points[-1].date }}</text></svg>
{% else %}<p class="empty">Add a body measurement to see your trend.</p>{% endif %}</article>
<article class="card"><h2>Recent workouts</h2>{% for row in workouts %}<div class="record"><strong>{{ row.exercise }} · {{ row.sets }} sets · {{ row.set_summary }}</strong><small>{{ row.workout_date }}{% if row.notes %} · {{ row.notes }}{% endif %}</small></div>{% else %}<p class="empty">No workouts recorded yet. Ask ChatGPT to log one.</p>{% endfor %}</article>
<article class="card"><h2>Recent measurements</h2>{% for row in measurements %}<div class="record"><strong>{{ "%.1f"|format(row.weight_kg) }} kg{% if row.body_fat_pct is not none %} · {{ "%.1f"|format(row.body_fat_pct) }}% body fat{% endif %}</strong>
<small>{{ row.measured_on }}{% for label, value in [('Waist',row.waist_cm),('Chest',row.chest_cm),('Hips',row.hips_cm)] %}{% if value is not none %} · {{ label }} {{ "%.1f"|format(value) }} cm{% endif %}{% endfor %}</small></div>{% else %}<p class="empty">No measurements recorded yet.</p>{% endfor %}</article>
</section></main></body></html>
"""


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
    return render_template_string(LOGIN_PAGE, csrf=csrf_token())


@app.post("/logout")
@login_required
def logout():
    if not valid_csrf():
        abort(400)
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    db = get_db()
    measurements = db.execute(
        "SELECT * FROM measurements WHERE user_id = ? ORDER BY measured_on DESC, id DESC LIMIT 8",
        (g.user["id"],),
    ).fetchall()
    raw_workouts = db.execute(
        "SELECT * FROM workouts WHERE user_id = ? ORDER BY workout_date DESC, id DESC LIMIT 10",
        (g.user["id"],),
    ).fetchall()
    workouts = []
    for row in raw_workouts:
        item = dict(row)
        details = json.loads(row["sets_json"]) if row["sets_json"] else []
        item["set_summary"] = ", ".join(
            f'{one["reps"]} reps' + (f' @ {one["weight_kg"]:g} kg' if one.get("weight_kg") is not None else "")
            for one in details
        ) if details else f'{row["reps"]} reps' + (f' @ {row["weight_kg"]:g} kg' if row["weight_kg"] is not None else "")
        workouts.append(item)
    history = db.execute(
        "SELECT measured_on, weight_kg FROM measurements WHERE user_id = ? ORDER BY measured_on, id",
        (g.user["id"],),
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
    return render_template_string(
        DASHBOARD, user=g.user, csrf=csrf_token(), new_api_key=None, today=date.today().isoformat(),
        workouts=workouts, measurements=measurements, chart_points=chart_points,
        chart_min=chart_min, chart_max=chart_max, chart_polyline=polyline,
    )


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
        flash("Measurements saved.")
    except ValueError as exc:
        flash(str(exc))
    return redirect(url_for("index"))


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
    return render_template_string(
        DASHBOARD, user=g.user, csrf=csrf_token(), new_api_key=token, today=date.today().isoformat(),
        workouts=[], measurements=[], chart_points=[], chart_min=None, chart_max=None, chart_polyline="",
    )


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
    if not isinstance(payload, dict):
        return {"error": "Send a JSON object."}, 400
    try:
        workout_date = valid_date(payload.get("date") or date.today().isoformat())
        exercises = payload.get("exercises")
        if not isinstance(exercises, list) or not 1 <= len(exercises) <= 30:
            raise ValueError("Include between 1 and 30 exercises.")
        session_id = str(uuid.uuid4())
        rows = []
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
                reps = int(one.get("reps", 0))
                if not 1 <= reps <= 999:
                    raise ValueError("Reps must be between 1 and 999.")
                weight = number(one.get("weight_kg"), "Weight", 2000)
                rpe = number(one.get("rpe"), "RPE", 10, 0)
                if rpe is not None and rpe < 1:
                    raise ValueError("RPE must be between 1 and 10.")
                normalized.append({"reps": reps, "weight_kg": weight, "rpe": rpe})
            first = normalized[0]
            notes = str(exercise.get("notes", "")).strip()[:500]
            rows.append((g.api_user["id"], session_id, workout_date, name, len(normalized),
                         first["reps"], first["weight_kg"], notes, json.dumps(normalized)))
        db = get_db()
        db.executemany(
            """INSERT INTO workouts
               (user_id, session_id, workout_date, exercise, sets, reps, weight_kg, notes, sets_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        db.commit()
        return {"workout_id": session_id, "date": workout_date, "exercises_saved": len(rows)}, 201
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}, 400


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
