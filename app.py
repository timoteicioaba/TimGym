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
:root{color-scheme:dark;--bg:#090d0b;--surface:#111815;--surface-2:#17201b;--line:#26322b;--text:#f2f6f1;--muted:#9ba99f;--accent:#c7f36a;--accent-dim:#29371d;--blue:#a6d7ff}
*{box-sizing:border-box}
html{scroll-behavior:smooth;scroll-padding-top:24px}
body{margin:0;min-height:100vh;background:radial-gradient(ellipse at 50% -20%,#1c2a1f 0%,transparent 48%),var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.topbar{max-width:600px;margin:0 auto;padding:18px 20px 10px;display:flex;align-items:center;justify-content:space-between}
.brand{display:flex;align-items:center;gap:10px;font-size:1.08rem;font-weight:750;letter-spacing:-.03em}
.brand-mark{display:grid;place-items:center;width:34px;height:34px;border-radius:12px;background:var(--accent);color:#15200b;font-weight:900;font-size:1.05rem}
.user-chip{max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border:1px solid var(--line);border-radius:999px;padding:7px 12px;color:#d9e2db;font-size:.82rem}
.app-shell{max-width:600px;margin:0 auto;padding:18px 18px calc(105px + env(safe-area-inset-bottom))}
.welcome{padding:18px 3px 22px}
.eyebrow{color:var(--accent);font-size:.72rem;font-weight:750;letter-spacing:.13em;text-transform:uppercase}
.welcome h1{font-size:clamp(2rem,9vw,2.75rem);line-height:1.02;letter-spacing:-.065em;margin:10px 0 9px}
.welcome p{margin:0;color:var(--muted);font-size:.96rem}
.stats-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px;margin-bottom:14px}
.stat-card,.panel{background:linear-gradient(145deg,#141c17,#101613 78%);border:1px solid var(--line);border-radius:20px;box-shadow:0 12px 32px #00000022}
.stat-card{padding:17px 16px;min-height:125px;position:relative;overflow:hidden}
.stat-card:after{content:"";position:absolute;width:88px;height:88px;right:-32px;top:-32px;border-radius:50%;background:#c7f36a0c}
.stat-label{color:var(--muted);font-size:.76rem;font-weight:650;letter-spacing:.04em}
.stat-value{font-size:1.8rem;line-height:1.12;letter-spacing:-.06em;font-weight:760;margin:12px 0 5px}
.stat-unit{font-size:.85rem;color:var(--muted);font-weight:500;letter-spacing:0}
.stat-note{font-size:.78rem;color:var(--muted)}
.panel{padding:19px;margin:12px 0}
.panel-heading{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}
.panel h2{font-size:1.03rem;letter-spacing:-.025em;margin:0}
.panel-kicker{font-size:.74rem;color:var(--muted)}
.panel-title-mark{display:flex;align-items:center;gap:9px}
.panel-dot{width:8px;height:8px;border-radius:50%;background:var(--accent);box-shadow:0 0 12px #c7f36a65}
.chart{display:block;width:100%;height:auto;overflow:visible}
.chart text{fill:var(--muted);font:11px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
.chart-grid{stroke:#29352e;stroke-dasharray:3 5}
.chart-line{fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}
.chart-point{fill:var(--accent);stroke:#111815;stroke-width:2}
.fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:13px}
.fields.three{grid-template-columns:repeat(2,minmax(0,1fr))}
label{display:block;color:var(--muted);font-size:.78rem;font-weight:600;margin:0 0 6px}
input,textarea{width:100%;min-height:48px;border:1px solid #344239;border-radius:12px;padding:10px 12px;font:inherit;color:var(--text);background:#0b100d}
textarea{min-height:116px;resize:vertical}
input::placeholder,textarea::placeholder{color:#65736a}
input:focus,textarea:focus{outline:2px solid #c7f36a88;border-color:var(--accent)}
button{min-height:48px;border:0;border-radius:13px;padding:11px 17px;color:#16200c;background:var(--accent);font:700 .94rem -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer}
button:hover{filter:brightness(1.06)}
button.secondary{background:#202b24;color:var(--text);border:1px solid #344239}
.full-button{width:100%}
.muted{color:var(--muted);font-size:.85rem}
.flash{padding:12px 14px;border-radius:12px;background:#24331c;border:1px solid #455c2e;color:#e4f6c8;margin:0 0 14px}
.record{display:flex;gap:12px;align-items:flex-start;padding:13px 0;border-top:1px solid #253029}
.record:first-of-type{border-top:0;padding-top:0}
.record-icon{flex:0 0 34px;width:34px;height:34px;border-radius:11px;display:grid;place-items:center;background:var(--accent-dim);color:var(--accent);font-size:.85rem;font-weight:800}
.record-main{min-width:0;flex:1}
.record strong{display:block;font-size:.92rem;font-weight:680;overflow-wrap:anywhere}
.record small{display:block;color:var(--muted);font-size:.77rem;margin-top:3px}
.empty{padding:14px;border-radius:12px;color:var(--muted);background:#0c120f;font-size:.87rem}
.panel details summary{list-style:none;display:flex;justify-content:space-between;align-items:center;cursor:pointer;min-height:30px;font-weight:700}
.panel details summary::-webkit-details-marker{display:none}
.panel details summary:after{content:"＋";color:var(--accent);font-size:1.2rem}
.panel details[open] summary:after{content:"−"}
.panel details summary span{color:var(--muted);font-size:.8rem;font-weight:500}
.key-box{overflow-wrap:anywhere;background:#0b100d;border:1px solid var(--line);border-radius:12px;padding:13px;color:var(--accent);font: .85rem ui-monospace,monospace}
.flash + .key-box{margin-top:10px}
.bottom-nav{position:fixed;z-index:10;left:50%;transform:translateX(-50%);bottom:0;width:min(100%,600px);padding:9px 12px calc(10px + env(safe-area-inset-bottom));display:grid;grid-template-columns:repeat(4,1fr);gap:5px;background:#0c110feF;border-top:1px solid #28342d;backdrop-filter:blur(18px)}
.bottom-nav a{display:flex;min-height:48px;flex-direction:column;align-items:center;justify-content:center;gap:1px;color:#9eaaa2;font-size:.68rem}
.bottom-nav a:before{font-size:1.05rem;line-height:1.15;color:var(--accent)}
.bottom-nav a:nth-child(1):before{content:"⌂"}.bottom-nav a:nth-child(2):before{content:"⌁"}.bottom-nav a:nth-child(3):before{content:"＋"}.bottom-nav a:nth-child(4):before{content:"◉"}
.login-shell{max-width:440px;margin:20px auto}
.login-shell .panel{padding:22px}
.login-title{font-size:1.55rem;letter-spacing:-.05em;margin:0 0 6px}
@media(min-width:760px){.app-shell{padding-top:28px}.welcome{padding-top:30px}.stats-grid{gap:14px}.panel{padding:22px}.bottom-nav{bottom:18px;border:1px solid #28342d;border-radius:18px;padding-bottom:9px}}
@media(max-width:380px){.app-shell{padding-left:14px;padding-right:14px}.panel{padding:16px}.stat-card{padding:15px 13px}.stat-value{font-size:1.55rem}}
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
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#090d0b"><title>TimGym</title>""" + BASE_STYLE + """</head>
<body>
<header class="topbar">
  <div class="brand"><span class="brand-mark">T</span><span>TimGym</span></div>
  <div class="user-chip">{{ user.username }}</div>
</header>
<main id="top" class="app-shell">
{% for message in get_flashed_messages() %}<p class="flash">{{ message }}</p>{% endfor %}
{% if new_api_key %}<section class="panel"><div class="panel-heading"><h2>Your new ChatGPT key</h2></div><p class="muted">Copy it now; it is only shown once.</p><div class="key-box">{{ new_api_key }}</div></section>{% endif %}
<section class="welcome">
  <div class="eyebrow">{{ today_label }}</div>
  <h1>Your training,<br>in one place.</h1>
  <p>A clear look at your consistency and progress.</p>
</section>
<section class="stats-grid" aria-label="Your training at a glance">
  <article class="stat-card"><div class="stat-label">BODY WEIGHT</div>
    <div class="stat-value">{% if latest_weight is not none %}{{ "%.1f"|format(latest_weight) }}<span class="stat-unit"> kg</span>{% else %}—{% endif %}</div>
    <div class="stat-note">{% if weight_change is not none %}{% if weight_change > 0 %}+{% endif %}{{ "%.1f"|format(weight_change) }} kg since last check-in{% else %}Your latest check-in{% endif %}</div>
  </article>
  <article class="stat-card"><div class="stat-label">WORKOUTS · 7 DAYS</div>
    <div class="stat-value">{{ week_count }}</div><div class="stat-note">{% if week_count == 1 %}session logged{% else %}sessions logged{% endif %}</div>
  </article>
</section>
<section class="panel" id="trends">
  <div class="panel-heading"><div class="panel-title-mark"><span class="panel-dot"></span><h2>Weight trend</h2></div><span class="panel-kicker">Recent check-ins</span></div>
  {% if chart_points %}
  <svg class="chart" viewBox="0 0 700 210" role="img" aria-label="Body weight trend chart">
    <line class="chart-grid" x1="48" y1="50" x2="680" y2="50"/><line class="chart-grid" x1="48" y1="108" x2="680" y2="108"/><line class="chart-grid" x1="48" y1="166" x2="680" y2="166"/>
    <text x="2" y="54">{{ chart_max }} kg</text><text x="2" y="170">{{ chart_min }} kg</text>
    <polyline class="chart-line" points="{{ chart_polyline }}"/>
    {% for point in chart_points %}<circle class="chart-point" cx="{{ point.x }}" cy="{{ point.y }}" r="4"><title>{{ point.date }} · {{ "%.1f"|format(point.weight) }} kg</title></circle>{% endfor %}
    <text x="48" y="198">{{ chart_points[0].date }}</text><text x="620" y="198">{{ chart_points[-1].date }}</text>
  </svg>
  {% else %}<p class="empty">Your trend will appear after your first body-weight check-in.</p>{% endif %}
</section>
<section class="panel" id="workout-log">
  <div class="panel-heading"><div class="panel-title-mark"><span class="panel-dot"></span><h2>Log with ChatGPT</h2></div><span class="panel-kicker">iPhone Shortcut</span></div>
  <p class="muted" style="margin:0 0 13px">Describe your session naturally. ChatGPT will structure it, show you what it understood, and ask before saving.</p>
  <label for="workout-note">YOUR WORKOUT</label>
  <textarea id="workout-note" maxlength="4000" placeholder="Example: Squats 3 sets of 5 at 100 kg, then bench 3 × 8 at 60 kg."></textarea>
  <button class="full-button" id="run-workout-shortcut" type="button" style="margin-top:12px">Continue in ChatGPT Shortcut</button>
  <p class="panel-kicker" id="shortcut-handoff-status" role="status" style="margin:10px 0 0">Opens your “TimGym Upload” Shortcut. You can review before it saves.</p>
  <p class="panel-kicker" style="margin:8px 0 0"><a href="https://github.com/timoteicioaba/TimGym/blob/main/SHORTCUT.md" target="_blank" rel="noopener">Set up the Shortcut once</a> · requires an Apple Intelligence-compatible iPhone.</p>
</section>
<section class="panel" id="log">
  <details>
    <summary>Log a body check-in <span>Weight, body fat, measurements</span></summary>
    <form method="post" action="{{ url_for('add_measurement') }}" style="margin-top:18px">
      <input type="hidden" name="_csrf" value="{{ csrf }}">
      <div class="fields"><div><label for="measured_on">DATE</label><input id="measured_on" name="measured_on" type="date" value="{{ today }}" required></div>
      <div><label for="weight">BODY WEIGHT · KG</label><input id="weight" name="weight_kg" type="number" min="1" max="1000" step="0.1" placeholder="82.4" required></div></div>
      <div class="fields three"><div><label for="body_fat">BODY FAT · %</label><input id="body_fat" name="body_fat_pct" type="number" min="0" max="100" step="0.1" placeholder="Optional"></div>
      <div><label for="waist">WAIST · CM</label><input id="waist" name="waist_cm" type="number" min="1" max="500" step="0.1" placeholder="Optional"></div>
      <div><label for="chest">CHEST · CM</label><input id="chest" name="chest_cm" type="number" min="1" max="500" step="0.1" placeholder="Optional"></div></div>
      <div style="margin-bottom:14px"><label for="hips">HIPS · CM</label><input id="hips" name="hips_cm" type="number" min="1" max="500" step="0.1" placeholder="Optional"></div>
      <button class="full-button" type="submit">Save check-in</button>
    </form>
  </details>
</section>
<section class="panel" id="activity">
  <div class="panel-heading"><div class="panel-title-mark"><span class="panel-dot"></span><h2>Recent workouts</h2></div><span class="panel-kicker">Via ChatGPT</span></div>
  {% for row in workouts %}<div class="record"><span class="record-icon">↗</span><div class="record-main"><strong>{{ row.exercise }} <span class="muted">· {{ row.sets }} sets</span></strong><small>{{ row.set_summary }} · {{ row.workout_date }}{% if row.notes %} · {{ row.notes }}{% endif %}</small></div></div>
  {% else %}<p class="empty">Your workouts will show here after ChatGPT logs them.</p>{% endfor %}
</section>
<section class="panel" id="account">
  <div class="panel-heading"><div class="panel-title-mark"><span class="panel-dot"></span><h2>ChatGPT connection</h2></div></div>
  <p class="muted">Use a private key to connect your account to ChatGPT or an iPhone Shortcut. Keep it private.</p>
  <form method="post" action="{{ url_for('create_api_key') }}"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="secondary full-button" type="submit">Create or rotate personal key</button></form>
  <p class="panel-kicker" style="margin:12px 0 0"><a href="{{ url_for('openapi_spec') }}">Action API specification</a></p>
</section>
<section class="panel">
  <div class="panel-heading"><div class="panel-title-mark"><span class="panel-dot"></span><h2>Body measurements</h2></div><span class="panel-kicker">Latest</span></div>
  {% for row in measurements %}<div class="record"><span class="record-icon">◎</span><div class="record-main"><strong>{{ "%.1f"|format(row.weight_kg) }} kg{% if row.body_fat_pct is not none %} <span class="muted">· {{ "%.1f"|format(row.body_fat_pct) }}% fat</span>{% endif %}</strong><small>{{ row.measured_on }}{% for label, value in [('Waist',row.waist_cm),('Chest',row.chest_cm),('Hips',row.hips_cm)] %}{% if value is not none %} · {{ label }} {{ "%.1f"|format(value) }} cm{% endif %}{% endfor %}</small></div></div>
  {% else %}<p class="empty">No check-ins yet. Add one above when you're ready.</p>{% endfor %}
</section>
<form method="post" action="{{ url_for('logout') }}" style="padding:0 2px"><input type="hidden" name="_csrf" value="{{ csrf }}"><button class="secondary full-button" type="submit">Sign out</button></form>
</main>
<nav class="bottom-nav" aria-label="Main navigation">
<a href="#top">Home</a><a href="#trends">Trends</a><a href="#workout-log">Log</a><a href="#account">Account</a>
</nav>
<script>
document.getElementById("run-workout-shortcut").addEventListener("click", function () {
  const workout = document.getElementById("workout-note").value.trim();
  const status = document.getElementById("shortcut-handoff-status");
  if (!workout) {
    status.textContent = "Write a workout first.";
    return;
  }
  const shortcutUrl = "shortcuts://run-shortcut?name="
    + encodeURIComponent("TimGym Upload")
    + "&input=text&text=" + encodeURIComponent(workout);
  status.textContent = "Opening TimGym Upload…";
  window.location.href = shortcutUrl;
});
</script>
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
    latest_weight = measurements[0]["weight_kg"] if measurements else None
    weight_change = (
        measurements[0]["weight_kg"] - measurements[1]["weight_kg"]
        if len(measurements) > 1 else None
    )
    week_count = db.execute(
        "SELECT COUNT(*) FROM workouts WHERE user_id = ? AND workout_date >= date('now', '-6 days')",
        (g.user["id"],),
    ).fetchone()[0]
    return render_template_string(
        DASHBOARD, user=g.user, csrf=csrf_token(), new_api_key=None, today=date.today().isoformat(),
        today_label=date.today().strftime("%A · %B %d"),
        latest_weight=latest_weight, weight_change=weight_change, week_count=week_count,
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
        today_label=date.today().strftime("%A · %B %d"),
        latest_weight=None, weight_change=None, week_count=0,
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
