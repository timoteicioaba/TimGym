import hashlib
from datetime import date, timedelta
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_DATA = tempfile.mkdtemp()
os.environ["DATABASE_PATH"] = os.path.join(_DATA, "test.db")
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as timgym  # noqa: E402

KEY = "tg_test_key"
EXPORT = {"data": {"workouts": [{"id": "W1", "name": "Run", "start": "2026-10-21 07:00:00 +0300", "duration": 600}]}}


def setUpModule():
    timgym.create_user("tester", "a-long-password")
    db = timgym.connect_db()
    db.execute("INSERT INTO api_tokens (user_id, token_hash) VALUES (1, ?)", (hashlib.sha256(KEY.encode()).hexdigest(),))
    db.commit()
    db.close()


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = timgym.app.test_client()

    def post(self, body, headers=None, **kwargs):
        headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json", **(headers or {})}
        headers = {name: value for name, value in headers.items() if value is not None}
        data = body if isinstance(body, (str, bytes)) else json.dumps(body)
        return self.client.post("/api/health", data=data, headers=headers, **kwargs)

    def test_import_and_resend(self):
        for _ in range(2):
            response = self.post(EXPORT)
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["workouts_saved"], 1)
        db = timgym.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM health_workouts").fetchone()[0], 1)
        db.close()

    def test_import_summary_is_kept_for_settings(self):
        response = self.post({"data": {"workouts": [{"name": "Elliptical", "start": "yesterday"}], "metrics": []}})
        self.assertEqual(response.json["workouts_received"], 1)
        self.assertEqual(response.json["skipped_reasons"], ["Elliptical: unreadable start time 'yesterday'"])
        with self.client.session_transaction() as session:
            session["user_id"] = 1
        page = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("unreadable start time", page)

    def test_body_measurements_upsert_and_yield_to_manual_entries(self):
        def weigh(day, kg):
            return self.post({"data": {"metrics": [
                {"name": "weight_body_mass", "units": "kg", "data": [{"date": f"{day} 07:00:00 +0300", "qty": kg}]}]}})
        self.assertEqual(weigh("2026-09-01", 82.0).json["measurements_saved"], 1)
        self.assertEqual(weigh("2026-09-01", 81.6).json["measurements_saved"], 1)
        db = timgym.connect_db()
        rows = db.execute("SELECT weight_kg, source FROM measurements WHERE measured_on = '2026-09-01'").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(81.6, "apple_health")])

        # A manual check-in replaces the imported one and later syncs leave it alone.
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["_csrf"] = "token"
        self.client.post("/measurements", data={"_csrf": "token", "measured_on": "2026-09-01", "weight_kg": "80.9"})
        self.assertEqual(weigh("2026-09-01", 82.5).json["measurements_saved"], 0)
        rows = db.execute("SELECT weight_kg, source FROM measurements WHERE measured_on = '2026-09-01'").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(80.9, None)])
        db.close()

    def login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 1
            session["_csrf"] = "token"

    def test_today_prompts_to_log_an_unlogged_strength_session(self):
        today = date.today().isoformat()
        self.post({"data": {"workouts": [
            {"id": "today-lift", "name": "Traditional Strength Training", "start": f"{today} 18:00:00 +0000", "duration": 3000},
            {"id": "today-ride", "name": "Cycling", "start": f"{today} 07:00:00 +0000", "duration": 1800},
        ]}})
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertEqual(page.count("Log your exercises?"), 1)  # the ride is not a gym session
        self.assertIn("?session=", page)
        session_id = page.split("?session=")[1].split('"')[0]
        self.assertIn("Logging exercises for Traditional Strength Training", self.client.get(f"/log?session={session_id}").get_data(as_text=True))

        response = self.client.post("/workouts/save", headers={"X-CSRF-Token": "token"}, json={
            "date": today, "exercises": [{"name": "Squat", "sets": [{"reps": 5, "weight_kg": 100}]}]})
        self.assertEqual(response.status_code, 201)
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("Log your exercises?", page)
        self.assertIn("Traditional Strength Training", page)  # now shown as one merged session

    def test_today_shows_nutrition_and_body(self):
        today = date.today().isoformat()
        self.post({"data": {"metrics": [
            {"name": "dietary_energy", "units": "kcal", "data": [{"date": f"{today} 00:00:00 +0000", "qty": 2150}]},
            {"name": "protein", "units": "g", "data": [{"date": f"{today} 00:00:00 +0000", "qty": 160}]},
            {"name": "weight_body_mass", "units": "kg", "data": [{"date": f"{today} 07:00:00 +0000", "qty": 81.4}]},
        ]}})
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("2,150", page)
        self.assertIn("160<small> g</small>", page)
        self.assertIn("81.4", page)

    def test_nutrition_falls_back_to_the_latest_day_with_food(self):
        db = timgym.connect_db()
        db.execute("DELETE FROM health_daily")
        db.commit()
        db.close()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.post({"data": {"metrics": [
            {"name": "dietary_energy", "units": "kJ", "data": [{"date": f"{yesterday} 00:00:00 +0000", "qty": 8368}]},
            {"name": "protein", "units": "g", "data": [{"date": f"{yesterday} 00:00:00 +0000", "qty": 150}]},
            {"name": "carbohydrates", "units": "g", "data": [{"date": f"{yesterday} 00:00:00 +0000", "qty": 200}]},
            {"name": "total_fat", "units": "g", "data": [{"date": f"{yesterday} 00:00:00 +0000", "qty": 80}]},
        ]}})
        self.login()
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("No food logged today yet", page)
        self.assertIn("2,000", page)              # 8368 kJ
        self.assertIn("150<small> g</small>", page)
        self.assertIn('title="600 kcal">28% of calories', page)  # protein: 150 g × 4 = 600 of 2,120 kcal
        settings = self.client.get("/settings").get_data(as_text=True)
        self.assertIn('class="chip-small on">Nutrition', settings)

    def test_json_without_content_type(self):
        self.assertEqual(self.post(EXPORT, headers={"Content-Type": None}).status_code, 200)
        self.assertEqual(self.post(EXPORT, headers={"Content-Type": "text/plain"}).status_code, 200)

    def test_authorization_variants(self):
        for value in (f"Bearer {KEY}", f"bearer {KEY}", KEY, f"  Bearer   {KEY} "):
            self.assertEqual(self.post(EXPORT, headers={"Authorization": value}).status_code, 200, value)
        response = self.post(EXPORT, headers={"Authorization": None})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer YOUR_KEY", response.json["error"])
        self.assertEqual(self.post(EXPORT, headers={"Authorization": "Bearer wrong"}).status_code, 401)

    def test_errors_are_json(self):
        response = self.post("not json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON", response.json["error"])
        self.assertEqual(self.client.get("/api/health").status_code, 405)
        self.assertIn("POST", self.client.get("/api/health").json["error"])
        limit = timgym.app.config["MAX_CONTENT_LENGTH"]
        timgym.app.config["MAX_CONTENT_LENGTH"] = 100
        try:
            response = self.post({"data": {"workouts": [], "padding": "x" * 500}})
        finally:
            timgym.app.config["MAX_CONTENT_LENGTH"] = limit
        self.assertEqual(response.status_code, 413)
        self.assertIn("shorter date range", response.json["error"])


if __name__ == "__main__":
    unittest.main()
