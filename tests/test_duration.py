import unittest

from longcaster.duration import resolve_duration


class DurationTests(unittest.TestCase):
    def test_first_card_rounds_to_h3_grid(self):
        plan = resolve_duration(5.0)
        self.assertEqual(plan.generated_frames, 124)
        self.assertEqual(plan.actual_new_frames, 124)
        self.assertAlmostEqual(plan.actual_new_seconds, 124 / 24)

    def test_continuation_duration_means_new_timeline_time(self):
        plan = resolve_duration(5.0, context_frames=39)
        self.assertEqual(plan.generated_frames, 158)
        self.assertEqual(plan.actual_new_frames, 119)
        self.assertAlmostEqual(plan.actual_new_seconds, 119 / 24)

    def test_short_continuation_still_has_new_frames(self):
        plan = resolve_duration(0.1, context_frames=39)
        self.assertEqual(plan.generated_frames, 56)
        self.assertEqual(plan.actual_new_frames, 17)

    def test_invalid_duration_rejected(self):
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_duration(value)


if __name__ == "__main__":
    unittest.main()
