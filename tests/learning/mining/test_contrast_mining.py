"""Tesztek: learning/mining/contrast_mining.py."""
from datetime import datetime, timedelta
import math
import random
import unittest

from learning import MEASURES_BY_KEY, MiningParams, mine
from learning.mining.baseline import pattern_counts
from learning.mining.contrast_mining import benjamini_hochberg, binomial_sf, merge_equivalent
from learning.mining.profiles import build_profiles
from tests.support.manager_fixtures import counts, result
from tests.support.profile_fixtures import PICKUP, make_profile, synthetic_profiles
from tests.support.stint_fixtures import LEVELS, START, STINT_PICKUP, dispatched, week


class StatisticsTest(unittest.TestCase):
    def test_binomial_sf_matches_brute_force(self):
        for n, p in ((10, 0.1), (25, 0.3), (40, 0.05)):
            for k in (0, 1, 3, 7):
                exact = sum(math.comb(n, j) * p**j * (1 - p) ** (n - j) for j in range(k, n + 1))
                self.assertAlmostEqual(binomial_sf(k, n, p), exact, places=12)

    def test_benjamini_hochberg(self):
        q_values = benjamini_hochberg([0.01, 0.04, 0.03, 0.005])
        for actual, expected in zip(q_values, [0.02, 0.04, 0.04, 0.02]):
            self.assertAlmostEqual(actual, expected)

class MiningTest(unittest.TestCase):
    def test_finds_planted_pattern_first(self):
        results = mine(synthetic_profiles(seed=7, planted=True), PICKUP)
        self.assertTrue(results)
        self.assertEqual(results[0].key, "assignment_group=G3&weekday=Sun")
        top = results[0]
        self.assertGreater(top.lift, 5)
        self.assertEqual(top.distinct_days, 2)
        self.assertEqual(len(top.bad_incident_ids), top.bad_count)

    def test_no_findings_without_planted_pattern(self):
        for seed in range(5):
            self.assertEqual(mine(synthetic_profiles(seed=seed, planted=False), PICKUP), [], f"seed={seed}")

    def test_date_and_weekday_never_combined(self):
        params = MiningParams(min_lift=1.0, fdr_q=1.0, min_support=1)
        for result in mine(synthetic_profiles(seed=7, planted=True), PICKUP, params):
            self.assertFalse({"date", "weekday"} <= result.dimensions, result.key)

    def test_recurring_pattern_needs_distinct_days(self):
        results = mine(synthetic_profiles(seed=7, planted=True), PICKUP, MiningParams(min_distinct_days=3))
        self.assertNotIn("assignment_group=G3&weekday=Sun", [r.key for r in results])

    def test_general_pattern_explained_by_specialization_is_dropped(self):
        # A G csoport minden rossz ticketje hétfői: a "G" önmagában redundáns, a "G & hétfő" a lényeg
        start = datetime(2026, 8, 3)
        profiles = []
        for index in range(300):
            opened = start + timedelta(days=index % 7, hours=9)
            group = "G" if index % 3 == 0 else f"H{index % 5}"
            bad = group == "G" and opened.weekday() == 0
            profiles.append(make_profile(index, opened, group, "S", bad or index % 50 == 1))
        keys = [r.key for r in mine(profiles, PICKUP)]
        self.assertIn("assignment_group=G&weekday=Mon", keys)
        self.assertNotIn("assignment_group=G", keys)

    def test_complex_pattern_needs_improvement_over_parts(self):
        # Minden hétfői ticket rossz, csoporttól függetlenül: csak a "hétfő" a minta, a "G1 & hétfő" nem
        start = datetime(2026, 8, 3)
        profiles = [
            make_profile(index, start + timedelta(days=index % 7, hours=9), f"G{index % 4}", "S",
                         (start + timedelta(days=index % 7)).weekday() == 0)
            for index in range(280)
        ]
        keys = [r.key for r in mine(profiles, PICKUP)]
        self.assertIn("weekday=Mon", keys)
        self.assertFalse([key for key in keys if "&" in key and "weekday=Mon" in key])
        # a hétfői dátumok sem külön minták: a dátum a hét napját jelenti, és nem erősebb nála
        self.assertFalse([key for key in keys if "date=" in key], keys)

    def test_suppressed_dimension_never_stands_alone(self):
        # a G3 vasárnapi ticketjei rosszak, és mind impact=4: az impact önmagában nem lehet minta
        profiles = [make_profile(i, p.opened_at, p.dimensions["assignment_group"], p.dimensions["business_service"],
                                 p.measures["pickup_delay"] > 8,
                                 {"impact": "4" if p.dimensions["assignment_group"] == "G3" else "3"})
                    for i, p in enumerate(synthetic_profiles(seed=7, planted=True))]
        def keys(results):
            return [k for r in results for k in (r.key, *r.alternative_keys)]
        # szabad szerepben az impact (a G3-mal azonos halmazt írva le) a leírások közt van
        self.assertTrue([k for k in keys(mine(profiles, PICKUP)) if "impact=" in k])
        # csak szűkítőként nem erősít semmit (ugyanaz a halmaz): sehol nem jelenik meg
        suppressed = mine(profiles, PICKUP, MiningParams(suppressed=frozenset({"impact"})))
        self.assertFalse([k for k in keys(suppressed) if "impact=" in k])
        self.assertIn("assignment_group=G3&weekday=Sun", [r.key for r in suppressed])

    def test_date_pattern_kept_when_stronger_than_weekday(self):
        # Csak az egyik hétfő volt rossz: a dátum a lényeg, nem a hét napja
        start = datetime(2026, 8, 3)
        profiles = [
            make_profile(index, start + timedelta(days=index % 14, hours=9), f"G{index % 4}", "S",
                         index % 14 == 0)
            for index in range(420)
        ]
        keys = [r.key for r in mine(profiles, PICKUP)]
        self.assertIn("date=2026-08-03", keys)
        self.assertNotIn("weekday=Mon", keys)

class RefineContextTest(unittest.TestCase):
    """Tovább bontott minta (pl. szombat): a szűkítései a kontextushoz mérve számítanak kiugrónak."""

    @staticmethod
    def profiles(seed=5):
        # minden szombat lassú (40%), a G3 szombaton szinte mindig (90%); hétköznap 4%
        rng = random.Random(seed)
        start = datetime(2026, 8, 3)
        result = []
        for index in range(4000):
            opened = start + timedelta(days=rng.randrange(28), hours=rng.randrange(8, 18))
            group = f"G{rng.randrange(1, 9)}"
            saturday = opened.weekday() == 5
            rate = (0.9 if group == "G3" else 0.4) if saturday else 0.04
            result.append(make_profile(index, opened, group, f"S{rng.randrange(1, 7)}", rng.random() < rate))
        return result

    def test_children_measured_against_context(self):
        params = MiningParams(min_support=3, fdr_q=0.05)
        saturday = (("weekday", "Sat"),)
        plain = mine(self.profiles(), PICKUP, params)
        self.assertIn(saturday, [r.items for r in plain])
        refined = mine(self.profiles(), PICKUP, params, contexts=[saturday])
        children = [r for r in refined if set(saturday) <= set(r.items) and r.items != saturday]
        self.assertTrue(any(("assignment_group", "G3") in r.items for r in children))
        for r in children:
            self.assertEqual(r.extra["context"], "weekday=Sat")
            self.assertAlmostEqual(r.extra["context_lift"], r.rate / r.extra["context_rate"])
            self.assertGreater(r.extra["context_lift"], 1.0)
        # egy átlagos szombati csoport nem jelez a szombathoz képest
        self.assertFalse(any(("assignment_group", "G1") in r.items for r in children))

class EmphasizedDimensionTest(unittest.TestCase):
    def test_emphasized_single_values_get_own_family(self):
        rng = random.Random(11)
        start = datetime(2026, 8, 3)
        profiles = []
        for index in range(900):
            opened = start + timedelta(days=rng.randrange(7), hours=rng.randrange(8, 18))
            service = f"S{rng.randrange(1, 60)}"
            rate = 0.45 if service == "S7" else 0.05
            profiles.append(make_profile(index, opened, f"G{rng.randrange(1, 9)}", service, rng.random() < rate,
                                         {"location": f"L{rng.randrange(1, 30)}"}))
        strict = MiningParams(min_support=3, fdr_q=0.0001)
        found = lambda params: [r.items for r in mine(profiles, PICKUP, params)]
        self.assertNotIn((("business_service", "S7"),), found(strict))
        emphasized = MiningParams(min_support=3, fdr_q=0.0001, emphasized=frozenset({"business_service"}), emphasized_fdr_q=0.05)
        self.assertIn((("business_service", "S7"),), found(emphasized))
        # a kiemelés csak az egyelemes mintákra hat: a kombinációk a szigorú családban maradnak
        self.assertFalse(any(len(items) > 1 and items not in found(strict) for items in found(emphasized)))

class MergeTest(unittest.TestCase):
    def test_identical_ticket_sets_are_merged(self):
        a = result((("assignment_group", "G"), ("subcategory", "3")), ["I1", "I2", "I3"])
        b = result((("contact_type", "self"), ("subcategory", "3")), ["I1", "I2", "I3"])
        c = result((("business_service", "S"),), ["I4", "I5", "I6"])
        merged = merge_equivalent([a, b, c])
        self.assertEqual(len(merged), 2)
        head = next(r for r in merged if r.bad_incident_ids == ("I1", "I2", "I3"))
        self.assertEqual(head.key, "assignment_group=G&subcategory=3")
        self.assertEqual(head.alternative_keys, ("contact_type=self&subcategory=3",))

class StintMiningTest(unittest.TestCase):
    def test_slow_resolver_on_arrival_weekday_is_found(self):
        as_of = START + timedelta(days=14)
        profiles = build_profiles(week(), as_of, START, LEVELS)
        self.assertEqual(len(profiles.stints), 140)              # L1-szakaszok nélkül
        results = mine(profiles.stints, STINT_PICKUP, MiningParams())
        self.assertEqual(results[0].key, "assignment_group=A&weekday=Sat")
        self.assertEqual((results[0].bad_count, results[0].distinct_days), (10, 2))
        self.assertEqual(results[0].population, 140)

    def test_without_levels_l1_stints_dilute_the_population(self):
        profiles = build_profiles(week(), START + timedelta(days=14), START)
        self.assertEqual(len(profiles.stints), 280)

    def test_multi_valued_dimension_matches_any_visited_group(self):
        incidents = [dispatched(f"INC{i}", START + timedelta(hours=i), "A", 1, later="B") for i in range(3)]
        incidents += [dispatched(f"INC{i}", START + timedelta(hours=i), "C", 1) for i in range(3, 5)]
        profiles = build_profiles(incidents, START + timedelta(days=2), START, LEVELS)
        reassignments = MEASURES_BY_KEY["reassignments"]
        counts = pattern_counts(profiles.tickets, reassignments, (("assignment_group", "B"),), bad_threshold=1)
        self.assertEqual((counts["bad"], counts["size"], counts["population"]), (3, 3, 5))
        # a később érintett B csoport is minta lehet, nem csak a nyitáskori L1
        results = mine(profiles.tickets, reassignments, MiningParams(min_support=3, min_lift=1.0, fdr_q=1.0),
                       bad_threshold=1)
        keys = {key for r in results for key in (r.key, *r.alternative_keys)}
        self.assertIn("assignment_group=B", keys)


if __name__ == "__main__":
    unittest.main()
