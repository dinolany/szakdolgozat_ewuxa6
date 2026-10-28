import itertools
import math
import random
import unittest
from datetime import datetime, timedelta

from models.discovery import MEASURES_BY_KEY, MiningParams, TicketProfile, build_profile, mine
from models.discovery.baseline import freeze_baseline, non_overlapping, pattern_counts
from models.discovery.mining import benjamini_hochberg, binomial_sf
from models.history import HistoryLine
from models.incident import Incident

PICKUP = MEASURES_BY_KEY["pickup_delay"]
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def make_incident(incident_id, events):
    incident = Incident(incident_id)
    for timestamp, field, old, new in events:
        incident.apply_event(HistoryLine({
            "set": incident_id, "timestamp": timestamp, "field": field, "old_value": old, "new_value": new,
        }))
    return incident


def make_profile(index, opened_at, group, service, bad, extra=None):
    dimensions = {
        "assignment_group": group,
        "business_service": service,
        "date": opened_at.date().isoformat(),
        "weekday": WEEKDAYS[opened_at.weekday()],
        "hour": f"{opened_at.hour:02d}",
    }
    dimensions.update(extra or {})
    return TicketProfile(f"INC{index:05d}", opened_at, dimensions, {"pickup_delay": 20.0 if bad else 1.0})


def synthetic_profiles(seed, planted, count=700, base_bad=0.04):
    """Két hét szintetikus ticket; `planted` esetén a G3 csoport vasárnapi ticketjei mind rosszak."""
    rng = random.Random(seed)
    start = datetime(2026, 8, 3)  # hétfő
    profiles = []
    for index in range(count):
        opened = start + timedelta(days=rng.randrange(14), hours=rng.randrange(8, 18))
        group = f"G{rng.randrange(1, 9)}"
        service = f"S{rng.randrange(1, 7)}"
        is_planted = planted and group == "G3" and opened.weekday() == 6
        profiles.append(make_profile(index, opened, group, service, is_planted or rng.random() < base_bad))
    return profiles


class ProfileTest(unittest.TestCase):
    EVENTS = [
        ("2026-08-03T08:00:00", "state", None, "5"),
        ("2026-08-03T08:00:00", "assignment_group", "", "A"),
        ("2026-08-03T08:00:00", "business_service", "", "S1"),
        ("2026-08-03T08:00:00", "assigned_to", "", ""),
        ("2026-08-03T10:00:00", "state", "5", "3"),
        ("2026-08-03T12:00:00", "state", "3", "2"),
        ("2026-08-03T13:00:00", "assignment_group", "A", "B"),
        ("2026-08-03T14:00:00", "assignment_group", "B", "C"),
        ("2026-08-03T20:00:00", "assigned_to", "", "U1"),
        ("2026-08-03T21:00:00", "u_sla_escalated", "0", "1"),
    ]

    def test_measures_and_dimensions(self):
        profile = build_profile(make_incident("INC1", self.EVENTS), datetime(2026, 8, 3, 22))
        self.assertEqual(profile.opened_at, datetime(2026, 8, 3, 8))
        self.assertEqual(profile.measures["pickup_delay"], 12.0)
        self.assertEqual(profile.measures["hold_time"], 2.0)
        self.assertEqual(profile.measures["reassignments"], 2.0)
        self.assertEqual(profile.measures["sla_escalation"], 1.0)
        self.assertEqual(profile.dimensions["assignment_group"], ("A", "B", "C"))  # az érintett csoportok
        self.assertEqual(profile.dimensions["business_service"], "S1")
        self.assertEqual((profile.dimensions["weekday"], profile.dimensions["hour"]), ("Mon", "08"))
        self.assertEqual(profile.evidence["pickup_delay"][0]["new_value"], "U1")

    def test_as_of_hides_later_events(self):
        profile = build_profile(make_incident("INC1", self.EVENTS), datetime(2026, 8, 3, 11))
        self.assertEqual(profile.measures["pickup_delay"], 3.0)  # még nem vették fel: eddigi várakozás
        self.assertEqual(profile.measures["hold_time"], 1.0)     # 10:00 óta hold állapotban
        self.assertEqual(profile.measures["reassignments"], 0.0)
        self.assertEqual(profile.measures["sla_escalation"], 0.0)

    def test_closed_without_pickup_is_not_applicable(self):
        incident = make_incident("INC2", [
            ("2026-08-03T08:00:00", "state", None, "5"),
            ("2026-08-03T09:00:00", "state", "5", "6"),
        ])
        self.assertIsNone(build_profile(incident, datetime(2026, 8, 4)).measures["pickup_delay"])

    def test_incident_without_events(self):
        self.assertIsNone(build_profile(Incident("INC3"), datetime(2026, 8, 4)))


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


if __name__ == "__main__":
    unittest.main()


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
