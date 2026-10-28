"""Tesztek: model/diagnostics/ping_pong.py."""
from datetime import datetime
import unittest

from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.detectors.ping_pong import PingPongDiagnosis
from storage.knowledge_base import KnowledgeBase
from tests.support.diagnostic_fixtures import BOUNCING, L1, measured
from tests.support.profile_fixtures import make_incident
from tests.support.threshold_fixtures import BASE


class PingPongTest(unittest.TestCase):
    def test_l1_dispatch_is_not_counted_and_returns_are(self):
        unknown = measured(PingPongDiagnosis(), [BOUNCING], 10)["INC1"]
        known = measured(PingPongDiagnosis(), [BOUNCING], 10, L1)["INC1"]
        self.assertEqual(unknown.details["moves"], 5)
        self.assertEqual((known.details["moves"], known.details["returned_to_l1"]), (3, 1))
        self.assertEqual(known.issue_id, "ISSUE-PING_PONG-INC1")       # a régi azonosítóformátum
        self.assertIn("vissza az L1-be", known.description)

    def test_as_of_ignores_later_moves(self):
        self.assertEqual(measured(PingPongDiagnosis(), [BOUNCING], 5, L1), {})  # 5 órakor még csak 2 váltás

class SignalItemsTest(unittest.TestCase):
    def test_ticket_detector_describes_route_groups_and_service(self):
        m = measured(PingPongDiagnosis(), [BOUNCING], 10, L1)["INC1"]
        items = PingPongDiagnosis().signal_items("INC1", m.details)
        self.assertIn(("incident", "INC1"), items)
        self.assertIn(("assignment_group", "B"), items)
        self.assertIn(("route", "L1 > A > B > C > D"), items)

class PingPongWithOverrideTest(unittest.TestCase):
    EVENTS = [("2026-08-03T08:00:00", "state", None, "1"), ("2026-08-03T08:00:00", "assignment_group", "", "A"),
              ("2026-08-03T09:00:00", "assignment_group", "A", "G"), ("2026-08-03T10:00:00", "assignment_group", "G", "B"),
              ("2026-08-03T11:00:00", "assignment_group", "B", "G")]

    def measure(self, knowledge):
        ctx = MeasurementContext(Trigger.MANUAL, datetime(2026, 8, 4), [make_incident("I1", self.EVENTS)], [],
                                 thresholds=BASE, knowledge=knowledge.view())
        return PingPongDiagnosis().run(ctx).measurements

    def test_scoped_threshold_changes_the_outcome(self):
        kb = KnowledgeBase()
        self.assertEqual([m.subject for m in self.measure(kb) if m.exceeded], ["I1"])   # 3 váltás: alapból ping-pong
        override = kb.add_threshold_override("PING_PONG", [["assignment_group", "G"]], {"moderate_moves": 5}, note="a G útvonal szokásos")
        self.assertEqual([m.subject for m in self.measure(kb) if m.exceeded], [])         # a G-n át 5-ig rendben
        kb.revoke_threshold_override(override.override_id)
        self.assertEqual([m.subject for m in self.measure(kb) if m.exceeded], ["I1"])

    def test_measurement_records_which_threshold_decided(self):
        kb = KnowledgeBase()
        kb.add_threshold_override("PING_PONG", [["assignment_group", "G"]], {"extreme_moves": 9})
        measurement = next(m for m in self.measure(kb) if m.subject == "I1")
        self.assertIn("(kézi)", measurement.details["threshold_source"])
        self.assertEqual(measurement.details["thresholds_used"], {"extreme_moves": 9})
        self.assertEqual(measurement.severity.value, "EXCEEDED_MODERATE")   # 3 váltás: mérsékelt, nem extrém


if __name__ == "__main__":
    unittest.main()
