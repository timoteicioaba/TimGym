import os
import sqlite3
from datetime import date
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template_string, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-local-secret")
DB_PATH = Path(os.environ.get("DATABASE_PATH", "data/timgym.db"))

SCHEMA = """
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
"""

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TimGym</title>
  <style>
    :root { color-scheme: light; --ink:#17231f; --muted:#62716a; --line:#dce5df; --green:#176b4b; --pale:#edf5ef; }
    * { box-sizing:border-box } body { margin:0; background:#f5f7f4; color:var(--ink); font:16px/1.45 system-ui,sans-serif; }
    header { background:#102b20; color:white; padding:24px max(20px,calc((100vw - 1050px)/2)); }
    header h1 { margin:0; font-size:1.6rem } header p { margin:4px 0 0; color:#c7d8cd }
    main { max-width:1050px; padding:22px 18px 48px; margin:auto }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:18px; align-items:start }
    .card { background:white; border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 2px 8px #122b2010 }
    .wide { grid-column:1/-1 } h2 { font-size:1.12rem; margin:0 0 16px } h3 { font-size:1rem; margin:0 0 8px }
    label { display:block; color:var(--muted); font-size:.85rem; margin:0 0 5px }
    input,textarea { width:100%; min-height:42px; border:1px solid #cbd7cf; border-radius:8px; padding:9px 10px; font:inherit; color:var(--ink); background:#fff }
    textarea { min-height:76px; resize:vertical } input:focus,textarea:focus { outline:2px solid #a8d4ba; border-color:var(--green) }
    .fields { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; margin-bottom:12px }
    .fields.three { grid-template-columns:repeat(3,minmax(0,1fr)) }
    button { border:0; border-radius:8px; padding:11px 16px; color:white; background:var(--green); font:600 1rem system-ui; cursor:pointer }
    button:hover { background:#105438 } .muted { color:var(--muted); font-size:.9rem }
    .flash { padding:10px 12px; border-radius:8px; background:var(--pale); margin:0 0 14px }
    .record { padding:12px 0; border-top:1px solid var(--line) } .record:first-of-type { border-top:0; padding-top:0 }
    .record strong { display:block } .record small { color:var(--muted) }
    .chart { width:100%; height:auto; min-height:160px } .chart text { fill:var(--muted); font:12px system-ui }
    .empty { padding:12px; color:var(--muted); background:#f6f8f6; border-radius:8px }
    @media(max-width:700px) { .grid { grid-template-columns:1fr } .wide { grid-column:auto } .fields.three { grid-template-columns:repeat(2,minmax(0,1fr)) } }
  </style>
</head>
<body>
<header><h1>TimGym</h1><p>Your personal training log</p></header>
<main>
  {% for message in get_flashed_messages() %}<p class="flash">{{ message }}</p>{% endfor %}
  <section class="grid">
    <article class="card">
      <h2>Log a workout</h2>
      <form method="post" action="{{ url_for('add_workout') }}">
        <div class="fields">
          <div><label for="workout_date">Date</label><input id="workout_date" name="workout_date" type="date" value="{{ today }}" required></div>
          <div><label for="exercise">Exercise</label><input id="exercise" name="exercise" maxlength="80" placeholder="Squat" required></div>
        </div>
        <div class="fields three">
          <div><label for="sets">Sets</label><input id="sets" name="sets" type="number" min="1" max="99" value="3" required></div>
          <div><label for="reps">Reps per set</label><input id="reps" name="reps" type="number" min="1" max="999" value="8" required></div>
          <div><label for="weight">Weight (kg)</label><input id="weight" name="weight_kg" type="number" min="0" max="2000" step="0.1" placeholder="Optional"></div>
        </div>
        <div style="margin-bottom:12px"><label for="notes">Notes (optional)</label><textarea id="notes" name="notes" maxlength="500" placeholder="How did it feel?"></textarea></div>
        <button type="submit">Save workout</button>
      </form>
    </article>
    <article class="card">
      <h2>Record measurements</h2>
      <form method="post" action="{{ url_for('add_measurement') }}">
        <div class="fields">
          <div><label for="measured_on">Date</label><input id="measured_on" name="measured_on" type="date" value="{{ today }}" required></div>
          <div><label for="body_weight">Body weight (kg)</label><input id="body_weight" name="weight_kg" type="number" min="1" max="1000" step="0.1" placeholder="e.g. 82.4" required></div>
        </div>
        <div class="fields three">
          <div><label for="body_fat">Body fat (%)</label><input id="body_fat" name="body_fat_pct" type="number" min="0" max="100" step="0.1" placeholder="Optional"></div>
          <div><label for="waist">Waist (cm)</label><input id="waist" name="waist_cm" type="number" min="1" max="500" step="0.1" placeholder="Optional"></div>
          <div><label for="chest">Chest (cm)</label><input id="chest" name="chest_cm" type="number" min="1" max="500" step="0.1" placeholder="Optional"></div>
        </div>
        <div style="margin-bottom:12px"><label for="hips">Hips (cm)</label><input id="hips" name="hips_cm" type="number" min="1" max="500" step="0.1" placeholder="Optional"></div>
        <button type="submit">Save measurements</button>
      </form>
    </article>
    <article class="card wide">
      <h2>Body-weight trend</h2>
      {% if chart_points %}
      <svg class="chart" viewBox="0 0 700 210" role="img" aria-label="Body weight trend chart">
        <line x1="48" y1="170" x2="680" y2="170" stroke="#dce5df"/>
        <line x1="48" y1="25" x2="48" y2="170" stroke="#dce5df"/>
        <text x="4" y="30">{{ chart_max }} kg</text><text x="4" y="170">{{ chart_min }} kg</text>
        <polyline fill="none" stroke="#176b4b" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="{{ chart_polyline }}"/>
        {% for point in chart_points %}
        <circle cx="{{ point.x }}" cy="{{ point.y }}" r="4" fill="#176b4b"><title>{{ point.date }}: {{ point.weight }} kg</title></circle>
        {% endfor %}
        <text x="48" y="198">{{ chart_points[0].date }}</text><text x="620" y="198">{{ chart_points[-1].date }}</text>
      </svg>
      {% else %}<p class="empty">Add your first body measurement to see a trend here.</p>{% endif %}
    </article>
    <article class="card">
      <h2>Recent workouts</h2>
      {% for row in workouts %}
      <div class="record"><strong>{{ row.exercise }} · {{ row.sets }} × {{ row.reps }}{% if row.weight_kg is not none %} · {{ row.weight_kg:g }} kg{% endif %}</strong><small>{{ row.workout_date }}{% if row.notes %} · {{ row.notes }}{% endif %}</small></div>
      {% else %}<p class="empty">No workouts recorded yet.</p>{% endfor %}
    </article>
    <article class="card">
      <h2>Recent measurements</h2>
      {% for row in measurements %}
      <div class="record"><strong>{{ row.weight_kg:g }} kg{% if row.body_fat_pct is not none %} · {{ row.body_fat_pct:g }}% body fat{% endif %}</strong>
        <small>{{ row.measured_on }}{% for label, value in [('Waist', row.waist_cm), ('Chest', row.chest_cm), ('Hips', row.hips_cm)] %}{% if value is not none %} · {{ label }} {{ value:g }} cm{% endif %}{% endfor %}</small></div>
      {% else %}<p class="empty">No measurements recorded yet.</p>{% endfor %}
    </article>
  </section>
</main>
</body></html>
"""

def get_db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def optional_number(field, label, maximum):
    raw = request.form.get(field, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if value < 0 or value > maximum:
        raise ValueError(f"{label} is outside the allowed range.")
    return value


def valid_date(field):
    value = request.form.get(field, "")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError("Please enter a valid date.") from exc


@app.route("/")
def index():
    db = get_db()
    measurements = db.execute(
        "SELECT * FROM measurements ORDER BY measured_on DESC, id DESC LIMIT 8"
    ).fetchall()
    workouts = db.execute(
        "SELECT * FROM workouts ORDER BY workout_date DESC, id DESC LIMIT 10"
    ).fetchall()
    history = db.execute(
        "SELECT measured_on, weight_kg FROM measurements ORDER BY measured_on, id"
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
            chart_points.append({"x": round(x, 1), "y": round(y, 1),
                                 "weight": row["weight_kg"], "date": row["measured_on"]})
        chart_min, chart_max = round(low, 1), round(high, 1)
        polyline = " ".join(f'{p["x"]},{p["y"]}' for p in chart_points)
    else:
        chart_min = chart_max = None
        polyline = ""
    return render_template_string(
        PAGE, today=date.today().isoformat(), workouts=workouts,
        measurements=measurements, chart_points=chart_points,
        chart_min=chart_min, chart_max=chart_max, chart_polyline=polyline,
    )


@app.post("/workouts")
def add_workout():
    try:
        workout_date = valid_date("workout_date")
        exercise = request.form.get("exercise", "").strip()
        if not exercise or len(exercise) > 80:
            raise ValueError("Enter an exercise name (up to 80 characters).")
        sets = int(request.form.get("sets", ""))
        reps = int(request.form.get("reps", ""))
        if not 1 <= sets <= 99 or not 1 <= reps <= 999:
            raise ValueError("Sets or reps are outside the allowed range.")
        weight = optional_number("weight_kg", "Weight", 2000)
        notes = request.form.get("notes", "").strip()[:500]
        db = get_db()
        db.execute(
            "INSERT INTO workouts (workout_date, exercise, sets, reps, weight_kg, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (workout_date, exercise, sets, reps, weight, notes),
        )
        db.commit()
        flash("Workout saved.")
    except (ValueError, TypeError):
        flash("Could not save workout. Check the date and numbers.")
    return redirect(url_for("index"))


@app.post("/measurements")
def add_measurement():
    try:
        measured_on = valid_date("measured_on")
        weight = optional_number("weight_kg", "Body weight", 1000)
        if weight is None or weight <= 0:
            raise ValueError("Body weight is required.")
        body_fat = optional_number("body_fat_pct", "Body fat", 100)
        waist = optional_number("waist_cm", "Waist", 500)
        chest = optional_number("chest_cm", "Chest", 500)
        hips = optional_number("hips_cm", "Hips", 500)
        db = get_db()
        db.execute(
            """INSERT INTO measurements
               (measured_on, weight_kg, body_fat_pct, waist_cm, chest_cm, hips_cm)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (measured_on, weight, body_fat, waist, chest, hips),
        )
        db.commit()
        flash("Measurements saved.")
    except (ValueError, TypeError):
        flash("Could not save measurements. Check the date and numbers.")
    return redirect(url_for("index"))


with app.app_context():
    get_db().executescript(SCHEMA)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
