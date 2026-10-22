import hashlib
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
