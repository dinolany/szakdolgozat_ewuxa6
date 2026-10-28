"""Tesztek: learning/mining/trend.py."""
from datetime import datetime, timedelta
import random
import unittest

from learning import MEASURES_BY_KEY
from learning.discovery_manager import DiscoveryManager
from learning.mining.trend import TrendParams, mine_trends
from learning.state.candidate import make_candidate_id
from tests.support.profile_fixtures import make_profile


PICKUP = MEASURES_BY_KEY["pickup_delay"]

START = datetime(2026, 6, 1)

def period(start, days, rates, seed, count=600):
    """Ticketek egy időszakban; `rates`: csoportonként a rossz arány."""
    rng = random.Random(seed)
    groups = sorted(rates)
    profiles = []
    for index in range(count):
        opened = start + timedelta(days=rng.randrange(days), hours=rng.randrange(8, 18))
        group = groups[index % len(groups)]
        profiles.append(make_profile(seed * 10000 + index, opened, group, "S", rng.random() < rates[group]))
    return profiles

BASE = {"A": 0.10, "B": 0.10, "C": 0.10, "D": 0.40}   # D tartósan rossz, de stabil

PARAMS = TrendParams(min_history=20)

def keys(results):
    return [r.key for r in results]

class TrendTest(unittest.TestCase):
    def setUp(self):
        self.history = period(START, 42, BASE, seed=1, count=2400)

    def test_own_degradation_is_found_but_stable_bad_group_is_not(self):
        now = period(START + timedelta(days=42), 7, {**BASE, "A": 0.45}, seed=2)
        results = mine_trends(now, self.history, PICKUP, PARAMS)
        self.assertIn("assignment_group=A", keys(results))
        self.assertNotIn("assignment_group=D", keys(results))     # mindig 40%: a saját múltjához képest rendben
        found = next(r for r in results if r.key == "assignment_group=A")
        self.assertEqual(found.kind, "trend")
        self.assertAlmostEqual(found.extra["history_rate"], 0.10, delta=0.03)

    def test_systemwide_worsening_does_not_alert(self):
        doubled = {group: min(1.0, rate * 2) for group, rate in BASE.items()}
        now = period(START + timedelta(days=42), 7, doubled, seed=3)
        self.assertEqual(mine_trends(now, self.history, PICKUP, PARAMS), [])

    def test_known_problem_is_excluded_from_the_history(self):
        # a múltban A is rossz volt egy ideig, de az egy elfogadott finding: nem lehet a "normális" része
        bad_past = period(START, 42, {**BASE, "A": 0.45}, seed=4, count=2400)
        known = {p.incident_id for p in bad_past if p.dimensions["assignment_group"] == "A" and p.measures["pickup_delay"] > 8}
        now = period(START + timedelta(days=42), 7, {**BASE, "A": 0.45}, seed=5)
        self.assertNotIn("assignment_group=A", keys(mine_trends(now, bad_past, PICKUP, PARAMS)))
        self.assertIn("assignment_group=A", keys(mine_trends(now, bad_past, PICKUP, PARAMS, exclude_history=known)))

    def test_trend_candidate_is_separate_from_contrast_candidate(self):
        now = period(START + timedelta(days=42), 7, {**BASE, "A": 0.45}, seed=2)
        [trend] = [r for r in mine_trends(now, self.history, PICKUP, PARAMS) if r.key == "assignment_group=A"]
        manager = DiscoveryManager()
        day = START + timedelta(days=49)
        manager.ingest([trend], day, day - timedelta(days=7), day)
        candidate = manager.candidate(make_candidate_id("pickup_delay", "trend|assignment_group=A"))
        self.assertEqual((candidate.kind, candidate.key), ("trend", "assignment_group=A"))
        self.assertNotIn(make_candidate_id("pickup_delay", "assignment_group=A"), manager.state["candidates"])
        self.assertIn("history_rate", candidate.stats)


if __name__ == "__main__":
    unittest.main()
