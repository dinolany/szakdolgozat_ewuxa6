"""Tesztek: learning/children/child_runner.py."""
import unittest

from learning.children.child_def import ChildDef
from learning.children.child_runner import run_child
from tests.support.child_fixtures import AS_OF, IMMEDIATE, quick_close


class RunTest(unittest.TestCase):
    def test_affected_vs_reference(self):
        timelines = {f"A{i}": quick_close(f"A{i}", 5) for i in range(8)}
        timelines.update({f"A{i}": quick_close(f"A{i}", 200) for i in range(8, 10)})
        timelines.update({f"R{i}": quick_close(f"R{i}", 300, work_notes=True) for i in range(10)})
        child = ChildDef("CHD-1", "Azonnali zárás", IMMEDIATE, "explain", 0.6)
        out = run_child(child, [(f"A{i}", 1) for i in range(10)], [(f"R{i}", 1) for i in range(10)],
                        timelines.get, {}, AS_OF.isoformat())
        self.assertEqual((out["affected_true"], out["affected_n"], out["reference_true"], out["reference_n"]), (8, 10, 0, 10))
        self.assertTrue(out["triggered"])
        self.assertIn("80%", out["text"])
        self.assertIn("magyarázat", out["text"])

class GroupMethodTest(unittest.TestCase):
    def test_grouping_shows_distribution_and_triggers_on_concentration(self):
        timelines = {f"A{i}": quick_close(f"A{i}", 10) for i in range(4)}
        timelines.update({f"R{i}": quick_close(f"R{i}", 10) for i in range(4)})
        child = ChildDef("CHD-X", "Ráadó csoport szerint", "elozo_csoport", threshold=0.6, method="group")
        out = run_child(child, [(k, 1) for k in timelines if k.startswith("A")], [(k, 0) for k in timelines if k.startswith("R")],
                        timelines.get, {}, "2026-08-20")
        self.assertTrue(out["triggered"])                                    # minden érintettet az L1 adott rá
        self.assertEqual(out["groups"][0]["value"], "L1")
        self.assertEqual((out["groups"][0]["share"], out["groups"][0]["reference_share"]), (1.0, 0.0))
        self.assertIn("L1 100%", out["text"])


if __name__ == "__main__":
    unittest.main()
