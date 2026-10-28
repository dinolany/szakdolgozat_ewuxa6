import unittest
from datetime import datetime, timedelta

from engine.issue_manager import IssueManager
from model.diagnostics.core import MeasurementContext, Trigger
from learning.pattern_finding_diagnosis import PatternFindingDiagnosis
from learning import MEASURES_BY_KEY, MiningParams, mine
from learning.baseline import pattern_counts
from learning.knowledge import Candidate, CandidateState, make_candidate_id
from learning.manager import DiscoveryManager
from learning.profile import build_profiles, profile_from_timeline, stint_profiles
from learning.promotion import promote_finding
from model.knowledge.knowledge_base import KnowledgeView
from model.timeline.ticket_timeline import incident_timeline
from tests.test_discovery import make_incident

START = datetime(2026, 8, 3)  # hétfő
LEVELS = {"L1": "L1"}
STINT_PICKUP = MEASURES_BY_KEY["stint_pickup"]


def dispatched(incident_id, opened, target, pickup_hours, service="S1", later=None):
    """L1 nyitja, 10 perc múlva továbbítja `target`-nek, ott `pickup_hours` múlva veszik fel."""
    arrival = opened + timedelta(minutes=10)
    events = [
        (opened.isoformat(), "state", None, "1"),
        (opened.isoformat(), "assignment_group", "", "L1"),
        (opened.isoformat(), "business_service", "", service),
        (opened.isoformat(), "assigned_to", "", "agent"),
        (arrival.isoformat(), "assignment_group", "L1", target),
        (arrival.isoformat(), "assigned_to", "agent", ""),
        ((arrival + timedelta(hours=pickup_hours)).isoformat(), "assigned_to", "", "resolver"),
    ]
    if later:
        moment = arrival + timedelta(hours=pickup_hours + 1)
        events.append((moment.isoformat(), "assignment_group", target, later))
    return make_incident(incident_id, events)


def week():
    """Két hét: az A csoport szombati érkezéseit 20 óra múlva veszik fel, minden mást 1 óra alatt."""
    incidents = []
    for index in range(140):
        day, target = index % 14, ("A" if (index // 14) % 2 == 0 else "B")  # naponta 5 A és 5 B
        opened = START + timedelta(days=day, hours=8 + index % 6)
        slow = target == "A" and opened.weekday() == 5
        incidents.append(dispatched(f"INC{index:03d}", opened, target, 20 if slow else 1))
    return incidents


class StintProfileTest(unittest.TestCase):
    def test_l1_stints_are_excluded_and_dimensions_are_at_arrival(self):
        incident = dispatched("INC1", datetime(2026, 8, 7, 23, 55), "A", 3, later="B")  # péntek éjjel
        timeline = incident_timeline(incident, datetime(2026, 8, 10))
        self.assertEqual(len(stint_profiles(timeline)), 3)       # L1 ismeretlen: minden szakasz
        a, b = stint_profiles(timeline, LEVELS)
        self.assertEqual((a.dimensions["assignment_group"], a.dimensions["weekday"]), ("A", "Sat"))
        self.assertEqual(a.unit_id, "INC1#1")
        self.assertEqual(a.measures["stint_pickup"], 3.0)
        # B-nél még senki nem vette fel: a várakozás eddigi ideje
        self.assertEqual(b.dimensions["assignment_group"], "B")
        self.assertGreater(b.measures["stint_pickup"], 40)

    def test_unassigned_transferred_stint_has_no_pickup(self):
        incident = make_incident("INC1", [
            ("2026-08-03T08:00:00", "assignment_group", "", "A"),
            ("2026-08-03T09:00:00", "assignment_group", "A", "B"),
        ])
        a, _ = stint_profiles(incident_timeline(incident, datetime(2026, 8, 4)))
        self.assertIsNone(a.measures["stint_pickup"])

    def test_ticket_dimensions_list_visited_groups_and_l1_dispatch_is_not_ping_pong(self):
        timeline = incident_timeline(dispatched("INC1", START, "A", 1, later="B"), datetime(2026, 8, 5))
        unknown = profile_from_timeline(timeline)
        known = profile_from_timeline(timeline, LEVELS)
        self.assertEqual(known.dimensions["assignment_group"], ("L1", "A", "B"))
        self.assertEqual(known.dimensions["business_service"], "S1")
        self.assertEqual(unknown.measures["reassignments"], 2.0)
        self.assertEqual(known.measures["reassignments"], 1.0)  # csak az A -> B


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


class LevelFactTest(unittest.TestCase):
    def test_level_fact_classifies_without_creating_a_rule(self):
        manager = DiscoveryManager()
        manager.state["meta"]["now"] = "2026-08-10T00:00:00"
        manager.add_fact("szint", {"csoport": "L1", "szint": "L1"})
        self.assertEqual(dict(manager.knowledge_view().levels), {"L1": "L1"})
        self.assertEqual(manager.rules(), [])

    def test_not_working_fact_applies_to_stint_arrival_weekday(self):
        from tests.test_knowledge import DAY, ingest, result
        manager = DiscoveryManager()
        a_sat = (("assignment_group", "A"), ("weekday", "Sat"))
        ingest(manager, [result(a_sat, ["I1", "I2", "I3"], measure="stint_pickup")], DAY)
        manager.add_fact("nem_dolgozik", {"csoport": "A", "nap": "Sat"})
        self.assertEqual(manager.candidate(make_candidate_id("stint_pickup", "assignment_group=A&weekday=Sat")).state,
                         CandidateState.FILTERED.value)


class StintFindingTest(unittest.TestCase):
    def test_stint_finding_is_measured_on_stints(self):
        incidents = week()
        diagnostic = PatternFindingDiagnosis()
        db = {"issues": {}, "issue_history": []}
        key = "assignment_group=A&weekday=Sat"
        ctx = MeasurementContext(Trigger.MANUAL, START + timedelta(days=12, hours=23), incidents, [],
                                 knowledge=KnowledgeView(levels=LEVELS))
        found = Candidate(make_candidate_id("stint_pickup", key), "stint_pickup", key, state=CandidateState.ACCEPTED.value)
        issue = promote_finding(db, IssueManager(), diagnostic, found, ctx, 8)
        self.assertEqual(issue.details["population"], 70)       # egy hét rezolver szakaszai
        self.assertEqual(issue.details["bad_count"], 5)          # az A csoport szombati érkezései
        self.assertEqual(len(issue.affected_objects), 5)


if __name__ == "__main__":
    unittest.main()
