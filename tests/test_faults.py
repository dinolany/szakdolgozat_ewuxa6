from datetime import datetime, timedelta
import unittest

from model.timeline.timeline_builder import build_timeline
from simulation.synthetic.compare import incidents_from_rows
from simulation.synthetic.faults import Fault, group_profiles, inject, pick_clean_group
from tests.test_synthetic import row


START = datetime(2026, 7, 6)  # hétfő
CUT = datetime(2026, 9, 1)


def tickets(count=10):
    """L1 nyitja, 10 perc múlva A-nak adja, A 1 óra múlva felveszi, munkajegyzetet ír, és lezárja."""
    rows = []
    for index in range(count):
        opened = START + timedelta(days=index, hours=9)
        incident = f"SYN{index:03d}"
        rows += [
            row(incident, opened, "state", None, "1"),
            row(incident, opened, "assignment_group", "", "L1"),
            row(incident, opened + timedelta(minutes=10), "assignment_group", "L1", "A"),
            row(incident, opened + timedelta(minutes=70), "assigned_to", "", "a1"),
            row(incident, opened + timedelta(minutes=80), "work_notes", "", "megnéztem"),
            row(incident, opened + timedelta(hours=3), "state", "2", "6"),
        ]
    return rows


def stint_of(rows, incident, group):
    [incident_obj] = [i for i in incidents_from_rows(rows) if i.id == incident]
    timeline = build_timeline(incident, incident_obj.history, CUT)
    return next(s for s in timeline.stints if s.group == group)


class FaultInjectionTest(unittest.TestCase):
    def inject(self, fault):
        return inject(tickets(), [fault], START, CUT, l1_groups=["L1"], seed=1)

    def test_slow_pickup_delays_the_group_pickup_and_later_events(self):
        rows, manifest = self.inject(Fault("lassu", "slow_pickup", group="A", start_week=0, weeks=4, intensity=1.0, delay_hours=24))
        self.assertEqual(manifest["scenarios"][0]["applied"], 10)
        stint = stint_of(rows, "SYN000", "A")
        self.assertEqual((stint.first_assignment.at - stint.start), timedelta(hours=25))
        self.assertEqual(stint.end_reason, "closed")                 # a lezárás is eltolódott, a sorrend megmaradt

    def test_ping_pong_inserts_a_bounce(self):
        fault = Fault("pp", "ping_pong", group="A", start_week=0, weeks=4, intensity=1.0)
        rows, manifest = inject(tickets() + [row("X1", START, "assignment_group", "", "B")], [fault], START, CUT, ["L1"], 1)
        [incident] = [i for i in incidents_from_rows(rows) if i.id == "SYN000"]
        groups = [s.group for s in build_timeline("SYN000", incident.history, CUT).stints]
        self.assertEqual(groups, ["L1", "A", "B", "A"])

    def test_long_hold_and_dropped_feature(self):
        rows, _ = self.inject(Fault("hold", "long_hold", group="A", start_week=0, weeks=4, intensity=1.0, hold_hours=48))
        self.assertEqual(stint_of(rows, "SYN001", "A").hold_hours, 48.0)
        rows, manifest = self.inject(Fault("drop", "drop_feature", group="A", start_week=0, weeks=4, intensity=1.0))
        self.assertFalse([r for r in rows if r["field"] == "work_notes"])
        self.assertEqual(manifest["truth"][0]["removed"], 1)

    def test_dropped_feature_covers_every_visit(self):
        rows = [row("R1", START + timedelta(hours=h), f, o, n) for h, f, o, n in (
            (0, "assignment_group", "", "A"), (1, "work_notes", "", "x"), (2, "assignment_group", "A", "B"),
            (3, "assignment_group", "B", "A"), (4, "work_notes", "", "y"))]
        rows, _ = inject(rows, [Fault("drop", "drop_feature", group="A", start_week=0, weeks=4, intensity=1.0)], START, CUT, [], 1)
        self.assertFalse([r for r in rows if r["field"] == "work_notes"])

    def test_ramp_and_window(self):
        _, manifest = self.inject(Fault("ramp", "slow_pickup", group="A", shape="ramp", start_week=0, weeks=1, intensity=1.0))
        applied = [datetime.fromisoformat(t["arrival"]) for t in manifest["truth"]]
        self.assertTrue(all(START <= a < START + timedelta(weeks=1) for a in applied))   # csak a hiba időszakában
        self.assertLess(len(applied), 7)                                                  # a rámpa eleje gyenge


class CleanGroupTest(unittest.TestCase):
    def test_profiles_and_clean_choice(self):
        rows = tickets()
        for index in range(10):                       # B: ugyanannyi szakasz, de lassú felvétel, munkajegyzet nélkül
            opened = START + timedelta(days=index, hours=9)
            incident = f"B{index:03d}"
            rows += [row(incident, opened, "assignment_group", "", "L1"),
                     row(incident, opened + timedelta(minutes=10), "assignment_group", "L1", "B"),
                     row(incident, opened + timedelta(hours=20), "assigned_to", "", "b1"),
                     row(incident, opened + timedelta(hours=21), "state", "2", "6")]
        profiles = group_profiles(rows, ["L1"])
        self.assertEqual((profiles["A"]["slow_pickup"], profiles["B"]["slow_pickup"]), (0.0, 1.0))
        self.assertEqual((profiles["A"]["drop_feature"], profiles["B"]["drop_feature"]), (1.0, 0.0))
        self.assertEqual(pick_clean_group("slow_pickup", profiles, set(), min_stints=5), "A")
        self.assertEqual(pick_clean_group("drop_feature", profiles, set(), min_stints=5), "A")
        self.assertEqual(pick_clean_group("slow_pickup", profiles, {"A"}, min_stints=5), "B")


if __name__ == "__main__":
    unittest.main()
