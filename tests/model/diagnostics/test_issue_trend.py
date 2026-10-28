"""Tesztek: model/diagnostics/issue_trend.py."""
from datetime import timedelta
import unittest

from engine.issue_manager import IssueManager
from model.diagnostics.core import Trigger
from model.diagnostics.detectors.issue_trend import IssueTrendDiagnosis
from model.diagnostics.detectors.ping_pong import PingPongDiagnosis
from model.diagnostics.diagnostic_registry import DiagnosticRegistry
from tests.support.diagnostic_fixtures import L1, T0, at
from tests.support.profile_fixtures import make_incident


class IssueTrendTest(unittest.TestCase):
    def test_counts_active_issues_per_category(self):
        incidents = [make_incident(f"INC{i}", [(at(0), "assignment_group", "", "L1"), (at(1), "assignment_group", "L1", "A"),
                                               (at(2), "assignment_group", "A", "B"), (at(3), "assignment_group", "B", "C"),
                                               (at(4), "assignment_group", "C", "D")])
                     for i in range(6)]
        registry = DiagnosticRegistry([PingPongDiagnosis(), IssueTrendDiagnosis()])
        db = {"issues": {}, "issue_history": []}
        registry.execute(db, IssueManager(), Trigger.MANUAL, T0 + timedelta(hours=5), incidents, {}, knowledge=L1)
        trend = [i for i in db["issues"].values() if i.diagnostic_key == "ISSUE_TREND"]
        self.assertEqual([(i.subject, i.details["count"]) for i in trend], [("PING_PONG", 6)])


if __name__ == "__main__":
    unittest.main()
