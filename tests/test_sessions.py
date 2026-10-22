import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))
os.environ.setdefault("SECRET_KEY", "test")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import link_health_to_gym  # noqa: E402


def gym(day, logged_at, name="Gym"):
    return {"kind": "gym", "date": day, "logged_at": logged_at, "exercises": [{"name": name}], "health": None}


def health(activity, day, start, minutes=50):
    return {"kind": "health", "date": day, "activity": activity, "started_at": start, "duration_s": minutes * 60}


class LinkSessionsTests(unittest.TestCase):
    def test_same_day_strength_workout_merges_and_cardio_stays(self):
        log = gym("2026-10-21", "2026-10-21 16:10:00")
        lifting = health("Traditional Strength Training", "2026-10-21", "2026-10-21T18:00:00+03:00")
        elliptical = health("Elliptical", "2026-10-21", "2026-10-21T17:40:00+03:00", 15)
        rest = link_health_to_gym([log], [lifting, elliptical])
        self.assertIs(log["health"], lifting)
        self.assertEqual(rest, [elliptical])
        self.assertEqual(lifting["linked_exercises"], 1)

    def test_two_sessions_in_one_day_pair_by_time(self):
        morning_log = gym("2026-10-21", "2026-10-21 05:10:00", "Squat")
        evening_log = gym("2026-10-21", "2026-10-21 16:20:00", "Bench")
        morning = health("Functional Strength Training", "2026-10-21", "2026-10-21T07:00:00+03:00", 60)
        evening = health("Traditional Strength Training", "2026-10-21", "2026-10-21T18:00:00+03:00", 60)
        rest = link_health_to_gym([evening_log, morning_log], [evening, morning])
        self.assertEqual(rest, [])
        self.assertIs(morning_log["health"], morning)
        self.assertIs(evening_log["health"], evening)

    def test_log_written_the_next_day_still_matches(self):
        log = gym("2026-10-20", "2026-10-21 09:00:00")
        lifting = health("Traditional Strength Training", "2026-10-20", "2026-10-20T19:00:00+03:00")
        self.assertEqual(link_health_to_gym([log], [lifting]), [])
        self.assertIs(log["health"], lifting)

    def test_different_days_do_not_match(self):
        log = gym("2026-10-21", "2026-10-21 16:10:00")
        lifting = health("Traditional Strength Training", "2026-10-20", "2026-10-20T18:00:00+03:00")
        self.assertEqual(link_health_to_gym([log], [lifting]), [lifting])
        self.assertIsNone(log["health"])


if __name__ == "__main__":
    unittest.main()


class MacroTests(unittest.TestCase):
    def test_macro_breakdown(self):
        from app import macro_breakdown
        result = macro_breakdown({"protein_g": 150, "carbs_g": 200, "fat_g": 80})
        self.assertEqual(result["kcal"], 2120)
        self.assertEqual([(item["key"], item["kcal"], item["share"]) for item in result["items"]],
                         [("protein", 600, 28), ("carbs", 800, 38), ("fat", 720, 34)])
        self.assertIsNone(macro_breakdown({"protein_g": None}))
