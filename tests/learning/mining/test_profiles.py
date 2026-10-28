"""Tesztek: learning/mining/profiles.py."""
from datetime import datetime
import unittest

from learning import build_profile
from learning.mining.profiles import profile_from_timeline, stint_profiles
from model.incident import Incident
from model.timeline.timeline_builder import incident_timeline
from tests.support.profile_fixtures import make_incident
from tests.support.stint_fixtures import LEVELS, START, dispatched


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


if __name__ == "__main__":
    unittest.main()
