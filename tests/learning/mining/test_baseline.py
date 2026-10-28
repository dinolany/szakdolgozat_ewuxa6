"""Tesztek: learning/mining/baseline.py."""
from datetime import datetime, timedelta
import unittest

from learning.mining.baseline import freeze_baseline, non_overlapping, pattern_counts
from tests.support.profile_fixtures import PICKUP, synthetic_profiles


class BaselineTest(unittest.TestCase):
    @staticmethod
    def record(end_day, bad, size, population_bad=50, population=1000):
        end = datetime(2026, 8, 1) + timedelta(days=end_day)
        return {"window_start": (end - timedelta(days=7)).isoformat(), "window_end": end.isoformat(),
                "bad": bad, "size": size, "population_bad": population_bad, "population": population}

    def test_pattern_counts(self):
        profiles = synthetic_profiles(seed=7, planted=True)
        values = pattern_counts(profiles, PICKUP, (("assignment_group", "G3"), ("weekday", "Sun")))
        group = [p for p in profiles if p.dimensions["assignment_group"] == "G3" and p.dimensions["weekday"] == "Sun"]
        self.assertEqual((values["size"], values["bad"]), (len(group), len(group)))  # beültetve: mind rossz
        self.assertEqual(values["population"], len(profiles))

    def test_non_overlapping_prefers_latest(self):
        records = [self.record(day, 1, 10) for day in (7, 10, 14, 20, 21)]
        self.assertEqual([r["window_end"][:10] for r in non_overlapping(records)], ["2026-08-22", "2026-08-15", "2026-08-08"])

    def test_smoothing_pulls_small_samples_toward_one(self):
        small = freeze_baseline([self.record(7, 2, 5)], "", min_size=1, prior=10)
        large = freeze_baseline([self.record(7 * i, 80, 200) for i in range(1, 6)], "", min_size=1, prior=10)
        self.assertAlmostEqual(small["raw_lift"], 8.0)
        self.assertLess(small["lift"], 4.0)
        self.assertAlmostEqual(large["lift"], 8.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
