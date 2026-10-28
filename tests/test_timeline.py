from datetime import datetime
import unittest

from model.history_line import HistoryLine
from model.timeline.timeline_builder import build_timeline


T0 = datetime(2026, 8, 3, 8)  # hétfő


def at(hour, minute=0):
    return datetime(2026, 8, 3 + hour // 24, hour % 24, minute).isoformat()


def history(*events):
    return [HistoryLine({"set": "INC1", "timestamp": ts, "field": field, "old_value": old, "new_value": new})
            for ts, field, old, new in events]


# L1 nyitja, továbbítja A-nak, A holdba teszi, B-nek adja, B felveszi és lezárja
EVENTS = history(
    (at(8), "state", None, "1"),
    (at(8), "incident_state", None, "1"),
    (at(8), "assignment_group", "", "L1"),
    (at(8), "business_service", "", "S1"),
    (at(8, 5), "assigned_to", "", "agent1"),
    (at(9), "assignment_group", "L1", "A"),
    (at(9), "assigned_to", "agent1", ""),
    (at(11), "assigned_to", "", "a1"),
    (at(12), "state", "1", "3"),
    (at(12), "u_pending_reason", "", "waiting for user"),
    (at(15), "business_service", "S1", "S2"),
    (at(16), "u_assignment_group", "A", "B"),
    (at(20), "state", "3", "2"),
    (at(22), "assigned_to", "a1", "b1"),
    (at(30), "state", "2", "6"),
    (at(30), "close_code", "", "Solved"),
)


class TimelineTest(unittest.TestCase):
    def test_stints_follow_the_group_path(self):
        timeline = build_timeline("INC1", EVENTS, datetime(2026, 8, 5))
        self.assertEqual([(s.group, s.end_reason, s.next_group) for s in timeline.stints],
                         [("L1", "transfer", "A"), ("A", "transfer", "B"), ("B", "closed", "")])
        self.assertEqual(timeline.groups, ["L1", "A", "B"])
        self.assertTrue(timeline.is_closed)
        l1, a, b = timeline.stints
        self.assertEqual((l1.hours, a.hours, b.hours), (1.0, 7.0, 14.0))

    def test_pickup_is_measured_within_each_stint(self):
        l1, a, b = build_timeline("INC1", EVENTS, datetime(2026, 8, 5)).stints
        self.assertEqual(l1.first_assignment.new, "agent1")
        # az átadáskor törölt felelős nem felvétel; A-nál 11:00-kor vették fel
        self.assertEqual(a.first_assignment.at, datetime(2026, 8, 3, 11))
        self.assertEqual(b.first_assignment.new, "b1")

    def test_hold_is_clipped_to_stints(self):
        timeline = build_timeline("INC1", EVENTS, datetime(2026, 8, 5))
        self.assertEqual([h.hours for h in timeline.holds], [8.0])  # 12:00 -> 20:00
        l1, a, b = timeline.stints
        self.assertEqual((l1.hold_hours, a.hold_hours, b.hold_hours), (0.0, 4.0, 4.0))

    def test_as_of_hides_the_future(self):
        timeline = build_timeline("INC1", EVENTS, datetime(2026, 8, 3, 13))
        self.assertEqual([s.group for s in timeline.stints], ["L1", "A"])
        current = timeline.stints[-1]
        self.assertEqual((current.end_reason, current.end), ("open", datetime(2026, 8, 3, 13)))
        self.assertEqual(timeline.holds[0].open, True)
        self.assertEqual(current.hold_hours, 1.0)
        self.assertEqual(timeline.state, "3")
        self.assertIsNone(build_timeline("INC1", EVENTS, datetime(2026, 8, 3, 7)))

    def test_field_values_at_moments(self):
        timeline = build_timeline("INC1", EVENTS, datetime(2026, 8, 5))
        l1, a, b = timeline.stints
        self.assertEqual(timeline.first_value("business_service"), "S1")
        self.assertEqual(timeline.value_at("business_service"), "S2")
        # az L1 átadásakori állapot: a továbbítás pillanatának változásai nélkül
        handover = timeline.values_at(l1.end, before=True)
        self.assertEqual((handover["assignment_group"], handover["assigned_to"]), ("L1", "agent1"))
        self.assertEqual(timeline.stint_at(datetime(2026, 8, 3, 13)).group, "A")
        self.assertEqual([c.new for c in a.changes_of("business_service")], ["S2"])

    def test_admin_after_closing_belongs_to_the_closing_stint(self):
        b = build_timeline("INC1", EVENTS, datetime(2026, 8, 5)).stints[-1]
        self.assertEqual([c.new for c in b.changes_of("close_code")], ["Solved"])

    def test_transfer_after_closing_gives_a_non_negative_closed_stint(self):
        events = history((at(8), "assignment_group", "", "A"), (at(9), "state", "1", "6"),
                         (at(10), "assignment_group", "A", "B"))
        b = build_timeline("INC1", events, datetime(2026, 8, 4)).stints[-1]
        self.assertEqual((b.group, b.end_reason, b.hours), ("B", "closed", 0.0))

    def test_reassignment_to_same_group_is_not_a_new_stint(self):
        events = history((at(8), "assignment_group", "", "A"), (at(9), "assignment_group", "A", "A"),
                         (at(10), "assignment_group", "A", "B"), (at(11), "assignment_group", "B", "A"))
        timeline = build_timeline("INC1", events, datetime(2026, 8, 4))
        self.assertEqual([s.group for s in timeline.stints], ["A", "B", "A"])
        self.assertEqual(timeline.groups, ["A", "B"])


if __name__ == "__main__":
    unittest.main()


class SameMomentAssignmentTest(unittest.TestCase):
    """A továbbítás mentésében a felelős törlése vagy beállítása a fogadóé, a naplóbeli sorrendtől függetlenül."""

    def timeline(self, assignee_first):
        from datetime import datetime
        from model.timeline.timeline_builder import incident_timeline
        from tests.test_discovery import make_incident
        transfer = [("2026-08-03T10:00:00", "assignment_group", "A", "B")]
        assign = [("2026-08-03T10:00:00", "assigned_to", "", "b_user")]
        events = [("2026-08-03T08:00:00", "state", None, "1"), ("2026-08-03T08:00:00", "assignment_group", "", "A")]
        events += (assign + transfer) if assignee_first else (transfer + assign)
        return incident_timeline(make_incident("I1", events), datetime(2026, 8, 4))

    def test_assignee_in_transfer_save_belongs_to_receiver(self):
        for assignee_first in (True, False):
            a, b = self.timeline(assignee_first).stints
            self.assertIsNone(a.first_assignment, assignee_first)          # a küldő nem vette fel
            self.assertEqual(b.first_assignment.new, "b_user", assignee_first)
            self.assertEqual(b.first_assignment.at, b.start)

    def test_cleared_assignee_is_not_a_pickup(self):
        from datetime import datetime
        from model.timeline.timeline_builder import incident_timeline
        from tests.test_discovery import make_incident
        events = [("2026-08-03T08:00:00", "state", None, "1"), ("2026-08-03T08:00:00", "assignment_group", "", "A"),
                  ("2026-08-03T09:00:00", "assigned_to", "", "a_user"),
                  ("2026-08-03T10:00:00", "assigned_to", "a_user", ""),          # a naplóban a csoportváltás előtt
                  ("2026-08-03T10:00:00", "assignment_group", "A", "B")]
        a, b = incident_timeline(make_incident("I1", events), datetime(2026, 8, 4)).stints
        self.assertEqual(a.first_assignment.new, "a_user")
        self.assertIsNone(b.first_assignment)
