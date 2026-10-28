"""Tesztek: model/diagnostics/threshold_override.py."""
import unittest

from model.diagnostics.core.threshold_override import ThresholdOverride, resolve
from tests.support.threshold_fixtures import BASE


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


if __name__ == "__main__":
    unittest.main()
