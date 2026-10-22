import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as timgym  # noqa: E402

EXPORT = {"data": {
    "workouts": [{"id": "F1", "name": "Indoor Cycling", "start": "2026-10-10 18:00:00 +0300", "duration": 1800,
                  "activeEnergyBurned": {"qty": 1255.2, "units": "kJ"}}],
    "metrics": [
        {"name": "dietary_energy", "units": "kJ", "data": [{"date": "2026-10-10 00:00:00 +0300", "qty": 8368}]},
        {"name": "weight_body_mass", "units": "kg", "data": [{"date": "2026-10-10 00:00:00 +0300", "qty": 90.5}]},
        {"name": "sleep_analysis", "units": "hr", "data": [{"date": "2026-10-10 00:00:00 +0300", "totalSleep": 7.25, "deep": 1, "core": 4.25, "rem": 2}]},
    ],
}}


class FileImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        timgym.create_user("uploader", "a-long-password")
        db = timgym.connect_db()
        cls.user_id = db.execute("SELECT id FROM users WHERE username = 'uploader'").fetchone()[0]
        db.close()

    def setUp(self):
        self.client = timgym.app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = self.user_id
            session["_csrf"] = "token"

    def upload(self, content, name="export.json", csrf="token"):
        data = {"_csrf": csrf}
        if content is not None:
            data["export"] = (io.BytesIO(content), name)
        return self.client.post("/settings/import", data=data, content_type="multipart/form-data", follow_redirects=True)

    def count(self, table):
        db = timgym.connect_db()
        try:
            return db.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = ?", (self.user_id,)).fetchone()[0]
        finally:
            db.close()

    def test_import_and_reimport(self):
        body = json.dumps(EXPORT).encode()
        page = self.upload(body).get_data(as_text=True)
        self.assertIn("Imported 1 workouts, 1 days of activity and nutrition, 1 nights of sleep, 1 body check-ins.", page)
        self.upload(body)
        self.assertEqual([self.count(t) for t in ("health_workouts", "health_daily", "health_sleep", "measurements")], [1, 1, 1, 1])
        self.assertIn("· <span title=\"export.json\">file</span>", page)

    def test_rejections(self):
        self.assertIn("not valid JSON", self.upload(b"{nope").get_data(as_text=True))
        self.assertIn("Choose a Health Auto Export JSON file", self.upload(None).get_data(as_text=True))
        self.assertIn("Could not import that file", self.upload(b"[]").get_data(as_text=True))
        self.assertEqual(self.upload(b"{}", csrf="wrong").status_code, 400)

    def test_oversized_file(self):
        limit = timgym.MANUAL_IMPORT_LIMIT
        timgym.MANUAL_IMPORT_LIMIT = 200
        try:
            page = self.upload(json.dumps(EXPORT).encode()).get_data(as_text=True)
        finally:
            timgym.MANUAL_IMPORT_LIMIT = limit
        self.assertIn("over 100 MB", page)


if __name__ == "__main__":
    unittest.main()
