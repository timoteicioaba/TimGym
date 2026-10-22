import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workout_parser import parse_workout_note  # noqa: E402

TODAY = date(2026, 10, 22)  # a Thursday


def parse(note, **kwargs):
    result = parse_workout_note(note, today=TODAY, **kwargs)
    assert "workout" in result, result
    return result["workout"]


def summary(note, **kwargs):
    """[(name, [(reps, weight_kg), ...]), ...] for compact assertions."""
    return [
        (exercise["name"], [(one["reps"], one["weight_kg"]) for one in exercise["sets"]])
        for exercise in parse(note, **kwargs)["exercises"]
    ]


class SetNotationTests(unittest.TestCase):
    def assertParses(self, note, expected):
        self.assertEqual(summary(note), expected, note)

    def test_sets_by_reps_with_load(self):
        self.assertParses("Squat 3x5 @ 80 kg", [("Squat", [(5, 80.0)] * 3)])
        self.assertParses("bench 80kg 5x5", [("Bench", [(5, 80.0)] * 5)])
        self.assertParses("squat 100 3x5", [("Squat", [(5, 100.0)] * 3)])
        self.assertParses("leg press 200 3x12", [("Leg press", [(12, 200.0)] * 3)])

    def test_load_by_reps_per_set(self):
        self.assertParses("Bench 60x8, 70x6, 80x5", [("Bench", [(8, 60.0), (6, 70.0), (5, 80.0)])])
        self.assertParses(
            "Squat: 100kg x 5, 110kg x 3, 120kg x 1",
            [("Squat", [(5, 100.0), (3, 110.0), (1, 120.0)])],
        )
        self.assertParses("Squat 5 @ 100, 3 @ 110", [("Squat", [(5, 100.0), (3, 110.0)])])

    def test_triples(self):
        self.assertParses("Squat 100x5x3", [("Squat", [(5, 100.0)] * 3)])
        self.assertParses("lat pulldown 3 x 10 x 55", [("Lat pulldown", [(10, 55.0)] * 3)])

    def test_words(self):
        self.assertParses("squats 3 sets of 5 at 100kg", [("Squats", [(5, 100.0)] * 3)])
        self.assertParses("deadlift 140 for 5", [("Deadlift", [(5, 140.0)])])
        self.assertParses("100 for 3x5 squat", [("Squat", [(5, 100.0)] * 3)])
        self.assertParses("curls 8 reps for 3 sets 12kg", [("Curls", [(8, 12.0)] * 3)])

    def test_rep_lists(self):
        self.assertParses("OHP 40kg 8,8,7", [("OHP", [(8, 40.0), (8, 40.0), (7, 40.0)])])
        self.assertParses("Bench 5/5/5 @ 80", [("Bench", [(5, 80.0)] * 3)])

    def test_each_group_keeps_its_own_load(self):
        self.assertParses("Bench 3x5 @ 80, 2x3 @ 90", [("Bench", [(5, 80.0)] * 3 + [(3, 90.0)] * 2)])

    def test_units(self):
        self.assertParses("rows 2x5 @ 62,5kg", [("Rows", [(5, 62.5)] * 2)])
        self.assertParses("Bench 1x5 at 100 lb", [("Bench", [(5, 45.36)])])
        self.assertParses("dips 3x12 +20kg", [("Dips", [(12, 20.0)] * 3)])
        self.assertParses("Pull ups 3x10 bodyweight", [("Pull ups", [(10, None)] * 3)])


class StructureTests(unittest.TestCase):
    def test_multiple_exercises(self):
        self.assertEqual(
            summary("Did bench today, 4 sets of 8 with 70kg, then rows 3x10 at 50"),
            [("Bench", [(8, 70.0)] * 4), ("Rows", [(10, 50.0)] * 3)],
        )
        self.assertEqual(
            summary("Squat 3x5 100kg, Bench 3x8 70kg, Deadlift 1x5 150kg"),
            [("Squat", [(5, 100.0)] * 3), ("Bench", [(8, 70.0)] * 3), ("Deadlift", [(5, 150.0)])],
        )
        self.assertEqual(
            summary("Squat 3x5 @ 80 kg. Bench 3x8 @ 60"),
            [("Squat", [(5, 80.0)] * 3), ("Bench", [(8, 60.0)] * 3)],
        )
        self.assertEqual(
            summary("squat 3x5 and bench 3x8"),
            [("Squat", [(5, None)] * 3), ("Bench", [(8, None)] * 3)],
        )

    def test_and_inside_a_name(self):
        self.assertEqual(summary("clean and jerk 3x2 @ 80"), [("Clean and jerk", [(2, 80.0)] * 3)])

    def test_lines_without_a_name_continue_the_exercise(self):
        self.assertEqual(
            summary("Bench press\n60x10\n80x8\n90x5"),
            [("Bench press", [(10, 60.0), (8, 80.0), (5, 90.0)])],
        )

    def test_notes(self):
        workout = parse("Squat 3x5 100kg felt heavy\nBench 60x8\nFelt good overall")
        self.assertEqual(workout["exercises"][0]["notes"], "felt heavy")
        self.assertEqual(workout["exercises"][1]["notes"], "Felt good overall")

        workout = parse("Bench 60x8, 70x6, 80x5 felt strong\nSquat 3x5, 100kg")
        self.assertEqual(
            [(exercise["name"], exercise["notes"], len(exercise["sets"])) for exercise in workout["exercises"]],
            [("Bench", "felt strong", 3), ("Squat", "", 3)],
        )

    def test_name_ignores_modifiers(self):
        self.assertEqual(summary("incline db press 2x10 30kg each")[0][0], "Incline db press")
        self.assertEqual(summary("bench 3x8 RPE 8 70kg")[0][0], "Bench")

    def test_known_names_are_reused(self):
        self.assertEqual(summary("squats 3x5 100", known_exercises=["Squat"])[0][0], "Squat")


class DetailTests(unittest.TestCase):
    def test_rpe(self):
        for note in ("bench 3x8 RPE 8 70kg", "bench 3x8 @ 8 rpe 70kg"):
            sets = parse(note)["exercises"][0]["sets"]
            self.assertEqual([(one["reps"], one["weight_kg"], one["rpe"]) for one in sets], [(8, 70.0, 8.0)] * 3)

    def test_missing_details_leave_fields_to_fill(self):
        exercise = parse("bench 3 sets of 8-12 at 60")["exercises"][0]
        self.assertEqual([(one["reps"], one["weight_kg"]) for one in exercise["sets"]], [(None, 60.0)] * 3)
        self.assertIn("reps", exercise["missing_fields"])

        exercise = parse("pull ups")["exercises"][0]
        self.assertEqual([(one["reps"], one["weight_kg"]) for one in exercise["sets"]], [(None, None)])
        self.assertEqual(exercise["missing_fields"], ["sets", "reps"])

        exercise = parse("squat 100kg")["exercises"][0]
        self.assertEqual(exercise["default_weight_kg"], 100.0)
        self.assertEqual(exercise["sets"][0]["weight_kg"], 100.0)

    def test_negative_load_is_not_guessed(self):
        exercise = parse("assisted pull ups 3x8 -20kg")["exercises"][0]
        self.assertEqual([one["weight_kg"] for one in exercise["sets"]], [None] * 3)
        self.assertIn("weight_kg", exercise["missing_fields"])

    def test_dates(self):
        self.assertEqual(parse("2026-01-05 squat 3x5")["date"], "2026-01-05")
        self.assertEqual(parse("yesterday squat 3x5")["date"], "2026-10-21")
        self.assertEqual(parse("squat 3x5 2 days ago")["date"], "2026-10-20")
        self.assertEqual(parse("on monday squat 3x5")["date"], "2026-10-19")
        self.assertEqual(parse("squat 3x5")["date"], "2026-10-22")

    def test_clarifications(self):
        self.assertIn("clarification", parse_workout_note(""))
        self.assertIn("clarification", parse_workout_note("3x5 @ 80"))
        self.assertIn("clarification", parse_workout_note("2026-02-30 squat 3x5"))


if __name__ == "__main__":
    unittest.main()
