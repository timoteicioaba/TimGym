import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as timgym  # noqa: E402


class DeleteManualDataTests(unittest.TestCase):
    def test_keeps_apple_health_data_and_other_accounts(self):
        timgym.create_user("cleaner", "a-long-password")
        timgym.create_user("bystander", "a-long-password")
        db = timgym.connect_db()
        ids = {row["username"]: row["id"] for row in db.execute("SELECT id, username FROM users")}
        me, other = ids["cleaner"], ids["bystander"]
        for user_id in (me, other):
            db.execute("INSERT INTO workouts (user_id, workout_date, exercise, sets, reps) VALUES (?, '2026-10-21', 'Squat', 3, 5)", (user_id,))
            db.execute("INSERT INTO measurements (user_id, measured_on, weight_kg) VALUES (?, '2026-10-20', 80)", (user_id,))
        db.execute("INSERT INTO measurements (user_id, measured_on, weight_kg, source) VALUES (?, '2026-10-21', 79.5, 'apple_health')", (me,))
        db.execute("INSERT INTO health_workouts (user_id, external_id, workout_date, started_at, activity) VALUES (?, 'A', '2026-10-21', '2026-10-21T18:00:00+03:00', 'Run')", (me,))
        db.execute("INSERT INTO health_daily (user_id, day, metric, value) VALUES (?, '2026-10-21', 'kcal_in', 2000)", (me,))
        db.execute("INSERT INTO api_tokens (user_id, token_hash) VALUES (?, 'hash')", (me,))
        db.commit()

        counts = timgym.delete_manual_data("Cleaner")
        self.assertEqual(counts, {"gym exercises logged": 1, "body check-ins entered by hand": 1})

        count = lambda sql, *args: db.execute(sql, args).fetchone()[0]
        self.assertEqual(count("SELECT COUNT(*) FROM workouts WHERE user_id = ?", me), 0)
        self.assertEqual(count("SELECT COUNT(*) FROM measurements WHERE user_id = ?", me), 1)
        self.assertEqual(count("SELECT source FROM measurements WHERE user_id = ?", me), "apple_health")
        self.assertEqual(count("SELECT COUNT(*) FROM health_workouts WHERE user_id = ?", me), 1)
        self.assertEqual(count("SELECT COUNT(*) FROM health_daily WHERE user_id = ?", me), 1)
        self.assertEqual(count("SELECT COUNT(*) FROM api_tokens WHERE user_id = ?", me), 1)
        self.assertEqual(count("SELECT COUNT(*) FROM workouts WHERE user_id = ?", other), 1)
        self.assertEqual(count("SELECT COUNT(*) FROM measurements WHERE user_id = ?", other), 1)
        db.close()
        with self.assertRaises(ValueError):
            timgym.delete_manual_data("nobody")


if __name__ == "__main__":
    unittest.main()
