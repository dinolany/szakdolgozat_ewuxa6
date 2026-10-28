"""Tesztek: model/diagnostics/core/suppression.py (tanult elnyomás a detektorokban)."""
from dataclasses import replace
import unittest

from learning.discovery_manager import DiscoveryManager
from model.diagnostics.core.suppression import suppressing_rule
from model.diagnostics.detectors.ping_pong import PingPongDiagnosis
from tests.support.diagnostic_fixtures import BOUNCING, L1, measured
from tests.support.manager_fixtures import DAY


class SuppressionTest(unittest.TestCase):
    RULE = {"rule_id": "RULE-1", "conditions": [{"dim": "assignment_group", "values": ["B"]}], "measures": ["PING_PONG"]}

    def test_rule_matches_any_of_the_groups_on_the_route(self):
        items = (("incident", "INC1"), ("assignment_group", "A"), ("assignment_group", "B"))
        self.assertEqual(suppressing_rule("PING_PONG", items, [self.RULE]), "RULE-1")
        self.assertIsNone(suppressing_rule("FLAPPING_INCIDENT", items, [self.RULE]))     # másik detektorra nem hat

    def test_suppressed_ticket_opens_no_issue_in_real_time(self):
        knowledge = replace(L1, suppressions=(self.RULE,))
        self.assertIn("INC1", measured(PingPongDiagnosis(), [BOUNCING], 10, L1))
        self.assertEqual(measured(PingPongDiagnosis(), [BOUNCING], 10, knowledge), {})

    def test_manager_rule_matches_multi_valued_signal(self):
        manager = DiscoveryManager()
        manager.ingest_signals([{"diagnostic": "PING_PONG", "subject": "INC1", "details": {}, "severity": "EXCEEDED_MODERATE",
                                 "description": "", "affected": ["INC1"],
                                 "items": [("incident", "INC1"), ("assignment_group", "A"), ("assignment_group", "B")]}], DAY)
        self.assertEqual(len(manager.queue()), 1)
        manager.add_rule([{"dim": "assignment_group", "values": ["A"]}], measures=["PING_PONG"])   # nem az utolsó érték
        self.assertEqual(manager.queue(), [])


if __name__ == "__main__":
    unittest.main()
