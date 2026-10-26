import hashlib
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as timgym  # noqa: E402
import push  # noqa: E402

KEY = "tg_live_key"
SUBSCRIPTION = {"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "BPk", "auth": "au"}}


class LiveWorkoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        timgym.create_user("lifter", "a-long-password")
        timgym.create_user("stranger", "a-long-password")
        db = timgym.connect_db()
        ids = {row["username"]: row["id"] for row in db.execute("SELECT id, username FROM users")}
        cls.user_id, cls.other_id = ids["lifter"], ids["stranger"]
        db.execute("INSERT INTO api_tokens (user_id, token_hash) VALUES (?, ?)", (cls.user_id, hashlib.sha256(KEY.encode()).hexdigest()))
        db.commit()
        db.close()

    def setUp(self):
        db = timgym.connect_db()
        for table in ("live_sessions", "push_subscriptions", "workouts"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
        db.close()
        self.client = timgym.app.test_client()
        self.login(self.user_id)
        self.sent = []
        self.refuse = False
        patcher = mock.patch.object(timgym.push, "send", side_effect=self.fake_send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_send(self, vapid, subscriptions, payload, subject, errors=None):
        self.sent.append((len(subscriptions), payload, subject))
        if self.refuse:
            errors.append("Apple answered 403: {\"reason\":\"BadJwtToken\"}")
            return 0, []
        return len(subscriptions), []

    def login(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["_csrf"] = "token"

    def api(self, path, body=None):
        return self.client.post(path, json=body or {}, headers={"Authorization": f"Bearer {KEY}"})

    def post(self, path, body=None):
        return self.client.post(path, json=body or {}, headers={"X-CSRF-Token": "token"})

    def subscribe(self):
        return self.post("/push/subscribe", SUBSCRIPTION)

    def test_shortcut_start_notifies_once_and_reuses_the_session(self):
        self.subscribe()
        first = self.api("/api/live/start", {"activity": "Traditional Strength Training"})
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json["created"])
        self.assertEqual(first.json["notified_devices"], 1)
        self.assertTrue(first.json["url"].endswith(f'/live/{first.json["session_id"]}'))
        (count, payload, subject), = self.sent
        self.assertEqual(payload["title"], "Traditional Strength Training started")
        self.assertEqual(payload["url"], f'/live/{first.json["session_id"]}')
        self.assertTrue(subject.startswith("http"))

        again = self.api("/api/live/start")
        self.assertEqual(again.status_code, 200)
        self.assertEqual((again.json["session_id"], again.json["created"]), (first.json["session_id"], False))
        self.assertEqual(len(self.sent), 1)  # no second notification

        self.assertEqual(self.client.post("/api/live/start").status_code, 401)

    def test_old_sessions_are_not_reused(self):
        db = timgym.connect_db()
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        db.execute("INSERT INTO live_sessions (user_id, activity, started_at) VALUES (?, 'Strength', ?)", (self.user_id, old))
        db.commit()
        db.close()
        self.assertTrue(self.api("/api/live/start").json["created"])

    def test_log_sets_undo_and_finish(self):
        session_id = self.api("/api/live/start").json["session_id"]
        page = self.client.get(f"/live/{session_id}").get_data(as_text=True)
        self.assertIn("Add set", page)
        for body in ({"exercise": "Squat", "reps": 5, "weight_kg": 100}, {"exercise": "Squat", "reps": 5, "weight_kg": 100},
                     {"exercise": "Bench press", "reps": 8, "weight_kg": 60}, {"exercise": "squat", "reps": 3, "weight_kg": 110},
                     {"exercise": "Pull ups", "reps": 10, "weight_kg": None}):
            response = self.post(f"/live/{session_id}/sets", body)
            self.assertEqual(response.status_code, 200, response.json)
        exercises = response.json["exercises"]
        # Logged under the library's names, with photos.
        self.assertEqual([(e["name"], len(e["sets"])) for e in exercises],
                         [("Barbell Bench Press - Medium Grip", 1), ("Barbell Squat", 3), ("Pullups", 1)])
        self.assertTrue(all(e["image"].startswith("/catalog/image/") for e in exercises))
        exercises = self.post(f"/live/{session_id}/undo").json["exercises"]
        self.assertEqual([e["name"] for e in exercises], ["Barbell Bench Press - Medium Grip", "Barbell Squat"])
        self.assertEqual(self.post(f"/live/{session_id}/sets", {"exercise": "", "reps": 5}).status_code, 400)
        self.assertEqual(self.post(f"/live/{session_id}/sets", {"exercise": "Row", "reps": "lots"}).status_code, 400)

        response = self.client.post(f"/live/{session_id}/finish", data={"_csrf": "token"})
        self.assertEqual(response.status_code, 302)
        db = timgym.connect_db()
        rows = db.execute("SELECT workout_date, exercise, sets, catalog_id FROM workouts WHERE user_id = ? ORDER BY id", (self.user_id,)).fetchall()
        status = db.execute("SELECT status FROM live_sessions WHERE id = ?", (session_id,)).fetchone()[0]
        db.close()
        self.assertEqual([(row["exercise"], row["sets"], row["catalog_id"]) for row in rows],
                         [("Barbell Bench Press - Medium Grip", 1, "Barbell_Bench_Press_-_Medium_Grip"), ("Barbell Squat", 3, "Barbell_Squat")])
        self.assertEqual(rows[0]["workout_date"], timgym.local_today().isoformat())
        self.assertEqual(status, "finished")
        self.assertEqual(self.post(f"/live/{session_id}/sets", {"exercise": "Squat", "reps": 5}).status_code, 409)
        self.assertTrue(self.api("/api/live/start").json["created"])  # a finished session isn't reused

    def test_same_exercise_under_different_names_is_one_exercise(self):
        session_id = self.api("/api/live/start").json["session_id"]
        for body in ({"exercise": "squat", "reps": 5, "weight_kg": 100}, {"exercise": "Barbell Squat", "reps": 5, "weight_kg": 100},
                     {"exercise": "Back squats", "reps": 5, "weight_kg": 100},
                     {"exercise": "Whatever", "catalog_id": "Barbell_Squat", "reps": 5, "weight_kg": 100},
                     {"exercise": "My weird drill", "reps": 10}, {"exercise": "my weird drill", "reps": 10}):
            exercises = self.post(f"/live/{session_id}/sets", body).json["exercises"]
        self.assertEqual([(e["name"], len(e["sets"])) for e in exercises], [("Barbell Squat", 4), ("My weird drill", 2)])

    def test_choices_list_each_exercise_once(self):
        db = timgym.connect_db()
        for day, name in (("2026-10-01", "Squat"), ("2026-10-02", "Barbell Squat"), ("2026-10-03", "squats"), ("2026-10-04", "My drill")):
            db.execute("INSERT INTO workouts (user_id, workout_date, exercise, sets, reps, weight_kg) VALUES (?, ?, ?, 1, 5, 100)",
                       (self.user_id, day, name))
        db.commit()
        db.close()
        session_id = self.api("/api/live/start").json["session_id"]
        page = self.client.get(f"/live/{session_id}").get_data(as_text=True)
        self.assertEqual(page.count('class="chip"'), 2)
        self.assertIn('data-exercise="Barbell Squat" data-catalog-id="Barbell_Squat"', page)
        self.assertIn('data-exercise="My drill"', page)
        self.assertIn('"barbell squat": {"reps": 5', page)  # prefill under the name the page logs

    def test_finishing_without_sets_saves_nothing(self):
        session_id = self.api("/api/live/start").json["session_id"]
        self.client.post(f"/live/{session_id}/finish", data={"_csrf": "token"})
        db = timgym.connect_db()
        self.assertEqual(db.execute("SELECT COUNT(*) FROM workouts").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT status FROM live_sessions WHERE id = ?", (session_id,)).fetchone()[0], "discarded")
        db.close()

    def test_watch_end_reminds_with_the_set_count(self):
        self.assertIsNone(self.api("/api/live/end").json["session_id"])
        self.subscribe()
        session_id = self.api("/api/live/start").json["session_id"]
        self.post(f"/live/{session_id}/sets", {"exercise": "Squat", "reps": 5, "weight_kg": 100})
        result = self.api("/api/live/end").json
        self.assertEqual((result["session_id"], result["sets_logged"]), (session_id, 1))
        self.assertEqual(self.sent[-1][1]["body"], "Save your 1 logged set.")

    def test_sessions_are_private(self):
        session_id = self.api("/api/live/start").json["session_id"]
        self.login(self.other_id)
        self.assertEqual(self.client.get(f"/live/{session_id}").status_code, 404)
        self.assertEqual(self.post(f"/live/{session_id}/sets", {"exercise": "Squat", "reps": 5}).status_code, 404)

    def test_today_offers_to_resume(self):
        session_id = self.api("/api/live/start").json["session_id"]
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Workout in progress", page)
        self.assertIn(f"/live/{session_id}", page)

    def test_manual_start_from_log(self):
        response = self.client.post("/live/start", data={"_csrf": "token"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/live/", response.headers["Location"])

    def test_subscriptions_and_test_push(self):
        self.assertEqual(self.post("/push/test").status_code, 404)
        self.assertEqual(self.post("/push/subscribe", {"endpoint": "http://insecure", "keys": {}}).status_code, 400)
        self.assertEqual(self.subscribe().status_code, 200)
        self.assertEqual(self.subscribe().status_code, 200)  # same device again
        self.assertEqual(self.post("/push/test").json["delivered"], 1)
        self.refuse = True
        refused = self.post("/push/test")
        self.assertEqual(refused.status_code, 502)
        self.assertIn("BadJwtToken", refused.json["error"])
        self.refuse = False
        settings = self.client.get("/settings").get_data(as_text=True)
        self.assertIn("1 device enabled", settings)
        self.assertIn("/api/live/start", settings)

    def test_service_worker_is_served_for_the_whole_site(self):
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        self.assertIn("notificationclick", response.get_data(as_text=True))


class PushModuleTests(unittest.TestCase):
    def test_keys_are_created_once_and_gone_devices_reported(self):
        directory = tempfile.mkdtemp()
        first = push.load_or_create_vapid(directory)
        self.assertEqual(push.public_key(first), push.public_key(push.load_or_create_vapid(directory)))
        self.assertEqual(len(push.public_key(first)), 87)  # 65-byte uncompressed point, base64url

        gone_response = mock.Mock(status_code=410, text='{"reason":"Unregistered"}')
        def fake_webpush(info, **kwargs):
            if info["endpoint"].endswith("gone"):
                raise push.WebPushException("gone", response=gone_response)
        with mock.patch.object(push, "webpush", side_effect=fake_webpush):
            errors = []
            delivered, gone = push.send(first, [
                {"endpoint": "https://push.example/ok", "p256dh": "a", "auth": "b"},
                {"endpoint": "https://web.push.apple.com/gone", "p256dh": "a", "auth": "b"},
            ], {"title": "x"}, "https://gym.example", errors)
        self.assertEqual((delivered, gone), (1, ["https://web.push.apple.com/gone"]))
        self.assertEqual(errors, ['Apple answered 410: {"reason":"Unregistered"}'])


if __name__ == "__main__":
    unittest.main()
