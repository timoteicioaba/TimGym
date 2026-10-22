import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from health_import import normalize_health_payload  # noqa: E402

AUTO_EXPORT = {
    "data": {
        "workouts": [
            {
                "id": "A1B2",
                "name": "Outdoor Run",
                "start": "2026-10-21 07:02:10 +0300",
                "end": "2026-10-21 07:34:40 +0300",
                "duration": 1950,
                "activeEnergyBurned": {"qty": 1602.47, "units": "kJ"},
                "distance": {"qty": 3.2, "units": "mi"},
            },
            {
                "id": "C3D4",
                "name": "Traditional Strength Training",
                "start": "2026-10-21 23:30:00 +0300",
                "end": "2026-10-22 00:20:00 +0300",
                "duration": 3000,
                "activeEnergyBurned": {"qty": 310, "units": "kcal"},
            },
            {"name": "Broken", "start": "not a date"},
        ],
        "metrics": [
            {
                "name": "active_energy",
                "units": "kcal",
                "data": [
                    {"date": "2026-10-21 00:00:00 +0300", "qty": 300},
                    {"date": "2026-10-21 01:00:00 +0300", "qty": 150.5},
                    {"date": "2026-10-22 00:00:00 +0300", "qty": 90},
                ],
            },
            {"name": "step_count", "units": "count", "data": [{"date": "2026-10-21 00:00:00 +0300", "qty": 8123}]},
            {"name": "heart_rate", "units": "count/min", "data": [{"date": "2026-10-21 00:00:00 +0300", "Avg": 60}]},
        ],
    }
}


class HealthAutoExportTests(unittest.TestCase):
    def setUp(self):
        self.result = normalize_health_payload(AUTO_EXPORT)

    def test_workouts(self):
        run, lift = self.result["workouts"]
        self.assertEqual(run["external_id"], "A1B2")
        self.assertEqual(run["activity"], "Outdoor Run")
        self.assertEqual(run["workout_date"], "2026-10-21")
        self.assertEqual(run["duration_s"], 1950)
        self.assertEqual(run["active_kcal"], 383.0)  # 1602.47 kJ
        self.assertEqual(run["distance_km"], 5.15)
        # The local date of the start time, not UTC.
        self.assertEqual(lift["workout_date"], "2026-10-21")
        self.assertEqual(lift["active_kcal"], 310.0)
        self.assertIsNone(lift["distance_km"])

    def test_daily_metrics_add_up_and_others_are_ignored(self):
        self.assertEqual(self.result["daily"], {
            ("2026-10-21", "active_kcal"): 450.5,
            ("2026-10-22", "active_kcal"): 90.0,
            ("2026-10-21", "steps"): 8123.0,
        })

    def test_unreadable_entries_are_counted(self):
        self.assertEqual(self.result["skipped"], 1)


class SimpleFormatTests(unittest.TestCase):
    def test_simple_workout_and_daily(self):
        result = normalize_health_payload({
            "workouts": [{"name": "Cycling", "start": "2026-10-20T18:00:00+03:00", "duration_min": 45,
                          "active_kcal": 420, "distance_km": 18.4}],
            "daily": [{"date": "2026-10-20", "active_kcal": 610, "steps": 9000}],
        })
        (ride,) = result["workouts"]
        self.assertEqual(ride["duration_s"], 2700)
        self.assertEqual(ride["external_id"], "2026-10-20T18:00:00+03:00|Cycling")
        self.assertEqual(result["daily"], {("2026-10-20", "active_kcal"): 610.0, ("2026-10-20", "steps"): 9000.0})

    def test_duration_from_end_time(self):
        result = normalize_health_payload({"workouts": [
            {"name": "Walk", "start": "2026-10-20 08:00:00 +0000", "end": "2026-10-20 08:30:00 +0000"}]})
        self.assertEqual(result["workouts"][0]["duration_s"], 1800)

    def test_resent_workouts_collapse(self):
        workout = {"id": "X", "name": "Run", "start": "2026-10-20 08:00:00 +0000"}
        self.assertEqual(len(normalize_health_payload({"workouts": [workout, workout]})["workouts"]), 1)

    def test_unselected_sections_may_be_null(self):
        result = normalize_health_payload({"data": {"workouts": None, "metrics": None}})
        self.assertEqual((result["workouts"], result["daily"]), ([], {}))

    def test_rejects_bad_shapes(self):
        for payload in ([], {"workouts": {}}, {"workouts": [{}] * 20001}):
            with self.assertRaises(ValueError):
                normalize_health_payload(payload)


if __name__ == "__main__":
    unittest.main()
