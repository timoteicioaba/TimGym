import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as timgym  # noqa: E402
import exercise_catalog as catalog  # noqa: E402


class MatchTests(unittest.TestCase):
    def test_shorthand_and_plurals(self):
        expected = {
            "Bench": "Barbell Bench Press - Medium Grip", "squats": "Barbell Squat", "OHP": "Standing Military Press",
            "RDL": "Romanian Deadlift", "Pull-ups": "Pullups", "chin ups": "Chin-Up", "Dips": "Parallel Bar Dip",
            "Lat pulldown": "Wide-Grip Lat Pulldown", "Incline db press": "Incline Dumbbell Press",
            "Face pulls": "Face Pull", "Leg press": "Leg Press", "Barbell Squat": "Barbell Squat",
        }
        for typed, name in expected.items():
            self.assertEqual(catalog.match(typed)["name"], name, typed)

    def test_no_guessing(self):
        for typed in ("Strength training", "Gym session", "Bulgarian split squat", ""):
            self.assertIsNone(catalog.match(typed), typed)

    def test_search_while_typing(self):
        self.assertIn("Barbell Bench Press - Medium Grip", [e["name"] for e in catalog.search("benc", 3)])
        self.assertEqual(catalog.search(""), [])


class CatalogRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        timgym.create_user("cataloger", "a-long-password")
        db = timgym.connect_db()
        cls.user_id = db.execute("SELECT id FROM users WHERE username = 'cataloger'").fetchone()[0]
        db.close()

    def setUp(self):
        self.client = timgym.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["_csrf"] = "token"

    def test_search_requires_sign_in_and_returns_photos(self):
        results = self.client.get("/catalog/search?q=bench").json["results"]
        self.assertEqual(results[0]["name"], "Barbell Bench Press - Medium Grip")
        self.assertTrue(results[0]["image"].startswith("/catalog/image/"))
        self.assertEqual(timgym.app.test_client().get("/catalog/search?q=bench").status_code, 302)

    def test_photos_are_downloaded_once_and_cached(self):
        fake = mock.Mock(status_code=200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8jpeg")
        with mock.patch.object(timgym.requests, "get", return_value=fake) as get:
            first = self.client.get("/catalog/image/Barbell_Squat/0.jpg")
            second = self.client.get("/catalog/image/Barbell_Squat/0.jpg")
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(second.data, b"\xff\xd8jpeg")
        self.assertEqual(get.call_count, 1)
        self.assertTrue(get.call_args[0][0].endswith("/exercises/Barbell_Squat/0.jpg"))
        for path in ("/catalog/image/Not_An_Exercise/0.jpg", "/catalog/image/Barbell_Squat/9.jpg", "/catalog/image/..%2F..%2Fetc/0.jpg"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_parse_suggests_and_save_keeps_the_choice(self):
        parsed = self.client.post("/workouts/interpret", json={"note": "Bench 60x8\nMy weird drill 3x10"},
                                  headers={"X-CSRF-Token": "token"}).json["workout"]["exercises"]
        self.assertEqual((parsed[0]["name"], parsed[0]["catalog_id"]), ("Barbell Bench Press - Medium Grip", "Barbell_Bench_Press_-_Medium_Grip"))
        self.assertIsNone(parsed[1]["catalog"])

        response = self.client.post("/workouts/save", headers={"X-CSRF-Token": "token"}, json={"date": "2026-10-20", "exercises": [
            {"name": "Front squats", "catalog_id": "Front_Barbell_Squat", "sets": [{"reps": 5, "weight_kg": 80}]},
            {"name": "Squat", "sets": [{"reps": 5, "weight_kg": 100}]},
            {"name": "My weird drill", "catalog_id": "not-real", "sets": [{"reps": 10}]},
        ]})
        self.assertEqual(response.status_code, 201)
        db = timgym.connect_db()
        stored = db.execute("SELECT exercise, catalog_id FROM workouts WHERE user_id = ? AND workout_date = '2026-10-20' ORDER BY id", (self.user_id,)).fetchall()
        db.close()
        self.assertEqual([tuple(row) for row in stored],
                         [("Front squats", "Front_Barbell_Squat"), ("Squat", "Barbell_Squat"), ("My weird drill", None)])

    def test_history_groups_old_and_new_names(self):
        db = timgym.connect_db()
        db.execute("INSERT INTO workouts (user_id, workout_date, exercise, sets, reps, weight_kg) VALUES (?, '2026-09-01', 'Deadlift', 1, 5, 140)", (self.user_id,))
        db.execute("INSERT INTO workouts (user_id, workout_date, exercise, sets, reps, weight_kg, catalog_id) VALUES (?, '2026-10-01', 'Barbell Deadlift', 1, 5, 150, 'Barbell_Deadlift')", (self.user_id,))
        db.commit()
        db.close()
        page = self.client.get("/exercises/Deadlift").get_data(as_text=True)
        self.assertIn("Barbell Deadlift</h1>", page)
        self.assertEqual(page.count('<span class="row-title">'), 2)  # both sessions
        self.assertIn("/catalog/image/Barbell_Deadlift/0.jpg", page)
        self.assertIn("How to do it", page)


if __name__ == "__main__":
    unittest.main()
