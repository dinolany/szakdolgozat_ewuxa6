import unittest
from datetime import datetime

from models.diagnostics.core import MeasurementContext, Trigger
from models.diagnostics.core.threshold_overrides import ThresholdOverride, resolve
from models.diagnostics.ping_pong import PingPongDiagnosis
from models.knowledge_base import KnowledgeBase
from tests.test_discovery import make_incident

BASE = {"moderate_moves": 3, "extreme_moves": 5}


class ResolveTest(unittest.TestCase):
    def test_manual_beats_learned_and_specific_beats_general(self):
        overrides = [
            ThresholdOverride("T1", "PING_PONG", [["assignment_group", "G"]], {"moderate_moves": 6}, "tanult"),
            ThresholdOverride("T2", "PING_PONG", [["assignment_group", "G"]], {"moderate_moves": 5}, "kézi"),
            ThresholdOverride("T3", "PING_PONG", [["assignment_group", "G"], ["business_service", "S"]], {"extreme_moves": 9}, "kézi"),
            ThresholdOverride("T4", "FLAPPING_INCIDENT", [], {"moderate_moves": 99}, "kézi"),
        ]
        params, sources = resolve(BASE, overrides, "PING_PONG", [("assignment_group", "G"), ("business_service", "S")])
        self.assertEqual(params, {"moderate_moves": 5, "extreme_moves": 9})
        self.assertEqual(sources, {"moderate_moves": "T2 (kézi)", "extreme_moves": "T3 (kézi)"})
        params, sources = resolve(BASE, overrides, "PING_PONG", [("assignment_group", "H")])
        self.assertEqual((params, set(sources.values())), (BASE, {"alapértelmezett"}))


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
