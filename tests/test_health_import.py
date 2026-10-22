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


class TimestampTests(unittest.TestCase):
    def workout_date(self, start):
        result = normalize_health_payload({"workouts": [{"name": "Elliptical", "start": start}]})
        return result["workouts"][0]["workout_date"] if result["workouts"] else result["skipped_reasons"]

    def test_twelve_hour_clock(self):
        # iPhones set to a 12-hour clock export AM/PM, often with a narrow no-break space.
        self.assertEqual(self.workout_date("2026-10-21 6:05:12 PM +0300"), "2026-10-21")
        self.assertEqual(self.workout_date("2026-10-21 6:05:12 PM +0300"), "2026-10-21")
        self.assertEqual(self.workout_date("2026-10-21 12:30:00 AM +0300"), "2026-10-21")

    def test_other_forms(self):
        self.assertEqual(self.workout_date("2026-10-21T18:05:12Z"), "2026-10-21")
        self.assertEqual(self.workout_date("2026-10-21 18:05 +03:00"), "2026-10-21")
        self.assertEqual(self.workout_date("2026-10-21 18:05:12"), "2026-10-21")

    def test_unreadable_start_is_explained(self):
        self.assertEqual(self.workout_date("21/10/2026 18:05"), ["Elliptical: unreadable start time '21/10/2026 18:05'"])

    def test_received_counts(self):
        result = normalize_health_payload(AUTO_EXPORT)
        self.assertEqual(result["received"], {"workouts": 3, "metrics": ["active_energy", "step_count", "heart_rate"]})


class BodyMeasurementTests(unittest.TestCase):
    def test_body_metrics_become_daily_check_ins(self):
        result = normalize_health_payload({"data": {"metrics": [
            {"name": "weight_body_mass", "units": "lb", "data": [
                {"date": "2026-10-21 07:10:00 +0300", "qty": 181.0},
                {"date": "2026-10-21 21:00:00 +0300", "qty": 182.0}]},
            {"name": "body_fat_percentage", "units": "%", "data": [{"date": "2026-10-21 07:10:00 +0300", "qty": 0.185}]},
            {"name": "waist_circumference", "units": "in", "data": [{"date": "2026-10-20 07:10:00 +0300", "qty": 33}]},
            {"name": "weight_body_mass", "units": "kg", "data": [{"date": "2026-10-19 07:10:00 +0300", "qty": 3}]},
        ]}})
        self.assertEqual(result["body"], {
            "2026-10-21": {"weight_kg": 82.3, "body_fat_pct": 18.5},  # two weigh-ins averaged
            "2026-10-20": {"waist_cm": 83.8},
        })
        self.assertEqual(result["daily"], {})
        self.assertIn("weight_body_mass: unusable reading 3", result["skipped_reasons"])


class NutritionTests(unittest.TestCase):
    def test_nutrition_metrics_under_either_spelling(self):
        result = normalize_health_payload({"data": {"metrics": [
            {"name": "dietary_energy", "units": "kJ", "data": [{"date": "2026-10-21 00:00:00 +0300", "qty": 8368}]},
            {"name": "Protein", "units": "g", "data": [{"date": "2026-10-21 00:00:00 +0300", "qty": 150}]},
            {"name": "carbohydrates", "units": "g", "data": [{"date": "2026-10-21 00:00:00 +0300", "qty": 210}]},
            {"name": "Total Fat", "units": "g", "data": [{"date": "2026-10-21 00:00:00 +0300", "qty": 70}]},
            {"name": "heart_rate", "units": "count/min", "data": []},
        ]}})
        self.assertEqual(result["daily"], {
            ("2026-10-21", "kcal_in"): 2000.0, ("2026-10-21", "protein_g"): 150.0,
            ("2026-10-21", "carbs_g"): 210.0, ("2026-10-21", "fat_g"): 70.0,
        })
        self.assertEqual(result["unused_metrics"], ["heart_rate"])


class RealExportShapeTests(unittest.TestCase):
    """Mirrors the structure of an actual Health Auto Export v2 file (values made up)."""

    def test_v2_export_shape(self):
        result = normalize_health_payload({"data": {
            "workouts": [{
                "id": "45550693-6A6C-4B11-BBAC-A4863C81469B", "name": "Traditional Strength Training",
                "start": "2026-10-21 17:29:10 +0300", "end": "2026-10-21 18:21:00 +0300", "duration": 3110.4,
                "activeEnergyBurned": {"qty": 2443.0, "units": "kJ"}, "totalEnergy": {"units": "kJ", "qty": 2900.0},
                "activeEnergy": [{"units": "kJ", "source": "", "qty": 35.0, "date": "2026-10-21 17:29:00 +0300"}],
                "heartRateData": [], "isIndoor": True, "location": "Indoor", "distance": None,
            }],
            "metrics": [
                {"name": "dietary_energy", "units": "kJ", "data": [{"qty": 5502.0, "date": "2026-10-21 00:00:00 +0300", "source": "MyFitnessPal"}]},
                {"name": "active_energy", "units": "kJ", "data": [{"qty": 3224.0, "date": "2026-10-21 00:00:00 +0300", "source": "Watch"}]},
                {"name": "body_fat_percentage", "units": "%", "data": [{"qty": 35.8, "date": "2026-10-21 00:00:00 +0300", "source": "Scale"}]},
                {"name": "weight_body_mass", "units": "kg", "data": [{"qty": 115.8, "date": "2026-10-21 00:00:00 +0300", "source": "Scale"}]},
                {"name": "heart_rate", "units": "count/min", "data": [{"Max": 150, "Avg": 80, "Min": 50, "date": "2026-10-21 00:00:00 +0300"}]},
                {"name": "sleep_analysis", "units": "hr", "data": [{"totalSleep": 7.1, "date": "2026-10-21 00:00:00 +0300"}]},
            ],
        }})
        (workout,) = result["workouts"]
        self.assertEqual((workout["workout_date"], workout["duration_s"], workout["active_kcal"], workout["distance_km"]),
                         ("2026-10-21", 3110, 583.9, None))
        self.assertEqual(result["daily"], {("2026-10-21", "kcal_in"): 1315.0, ("2026-10-21", "active_kcal"): 770.6})
        self.assertEqual(result["body"], {"2026-10-21": {"weight_kg": 115.8, "body_fat_pct": 35.8}})
        self.assertEqual(result["unused_metrics"], ["heart_rate"])
        self.assertEqual(result["sleep"]["2026-10-21"]["total_h"], 7.1)
        self.assertEqual(result["skipped"], 0)


class SleepTests(unittest.TestCase):
    def test_nights_are_filed_under_the_morning_and_naps_add_up(self):
        result = normalize_health_payload({"data": {"metrics": [{"name": "sleep_analysis", "units": "hr", "data": [
            {"date": "2026-10-22 00:00:00 +0300", "totalSleep": 6.12, "deep": 0.98, "core": 3.34, "rem": 1.79, "awake": 0.17,
             "sleepStart": "2026-10-22 00:51:07 +0300", "sleepEnd": "2026-10-22 07:08:05 +0300"},
            {"date": "2026-10-22 00:00:00 +0300", "totalSleep": 0.5, "core": 0.5,
             "sleepStart": "2026-10-22 14:00:00 +0300", "sleepEnd": "2026-10-22 14:30:00 +0300"},
            {"date": "2026-10-21 00:00:00 +0300", "asleep": 420, "sleepStart": "2026-10-20 23:30:00 +0300"},
            {"date": "2026-10-20 00:00:00 +0300"},
        ]}]}})
        night = result["sleep"]["2026-10-22"]
        self.assertEqual((night["total_h"], night["deep_h"], night["core_h"], night["rem_h"]), (6.62, 0.98, 3.84, 1.79))
        self.assertEqual((night["start"], night["end"]), ("2026-10-22T00:51:07+03:00", "2026-10-22T14:30:00+03:00"))
        self.assertNotIn("2026-10-21", result["sleep"])  # 420 hours is not a night
        self.assertEqual(result["skipped"], 2)
        self.assertIn("sleep_analysis: use aggregation Days so each night arrives as one entry", result["skipped_reasons"])

    def test_minutes_are_converted(self):
        result = normalize_health_payload({"data": {"metrics": [{"name": "sleep_analysis", "units": "min", "data": [
            {"date": "2026-10-22 00:00:00 +0300", "totalSleep": 390}]}]}})
        self.assertEqual(result["sleep"]["2026-10-22"]["total_h"], 6.5)
