import unittest

from athena_wake.runtime import ThresholdTrigger


class ThresholdTests(unittest.TestCase):
    def test_only_fresh_predictions_count_and_below_threshold_resets_streak(self):
        trigger = ThresholdTrigger(0.5, 2, 0)
        self.assertFalse(trigger.update(.9, 320))
        self.assertFalse(trigger.update(None, 640))
        self.assertTrue(trigger.update(.8, 960))
        self.assertFalse(trigger.update(.9, 1280))
        self.assertFalse(trigger.update(.4, 1600))
        self.assertFalse(trigger.update(.9, 1920))
        self.assertTrue(trigger.update(.9, 2240))

    def test_cooldown_boundary_is_audio_time(self):
        trigger = ThresholdTrigger(cooldown=.08)
        self.assertTrue(trigger.update(.9, 1280))
        self.assertFalse(trigger.update(.9, 2559))
        self.assertTrue(trigger.update(.9, 2560))
        with self.assertRaises(ValueError):
            trigger.update(.9, 100)

    def test_invalid_settings_and_scores(self):
        for options in ({"threshold": True}, {"threshold": float("nan")},
                        {"threshold": 1.1}, {"consecutive": True},
                        {"consecutive": 0}, {"cooldown": -1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                ThresholdTrigger(**options)
        trigger = ThresholdTrigger()
        for score in (True, float("inf"), -1, 2):
            with self.subTest(score=score), self.assertRaises(ValueError):
                trigger.update(score, 0)


if __name__ == "__main__":
    unittest.main()
