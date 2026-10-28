"""Tesztek: engine/issue_manager.py."""
from datetime import datetime, timedelta
import unittest

from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.detectors.ping_pong import PingPongDiagnosis
from tests.support.diagnostic_fixtures import BOUNCING, L1, T0
from tests.support.profile_fixtures import make_incident


class KnownFaultRealTimeTest(unittest.TestCase):
    def test_confirmed_issue_reopens_in_the_batch_when_fault_returns_after_quiet(self):
        from engine.issue_manager import IssueManager
        from model.issue_state import IssueState
        diagnostic, manager, db = PingPongDiagnosis(), IssueManager(), {"issues": {}, "issue_history": []}
        run = lambda limit: manager.apply_run(db, diagnostic.run(MeasurementContext(
            Trigger.EVENT_BATCH, T0 + timedelta(hours=10), [BOUNCING], list(db["issues"].values()),
            thresholds={"moderate_moves": limit, "extreme_moves": 9}, knowledge=L1)), "köteg")
        run(3)
        issue = db["issues"]["ISSUE-PING_PONG-INC1"]
        issue.state = IssueState.RESOLVED
        issue.details["lesson"] = {"outcome": "confirmed"}
        run(3)                                                     # még túllép: nem csendesedett el, nem nyílik újra
        self.assertEqual(db["issues"]["ISSUE-PING_PONG-INC1"].state, IssueState.RESOLVED)
        run(5)                                                     # küszöb alatt: elcsendesedett (a jelző megmarad)
        self.assertIn("known_fault_quiet_since", db["issues"]["ISSUE-PING_PONG-INC1"].details)
        changes = run(3)                                           # újra túllép: ugyanebben a kötegben újranyílik
        reopened = db["issues"]["ISSUE-PING_PONG-INC1"]
        self.assertEqual(reopened.state, IssueState.ACTIVE)
        self.assertIn("known_fault_returned_at", reopened.details)
        self.assertEqual(reopened.details["lesson"]["outcome"], "confirmed")
        self.assertTrue(any(c.get("reason") == "ismert hiba visszatért" for c in changes))

class IssueKeepsAnalystDataTest(unittest.TestCase):
    def test_lesson_and_follow_up_survive_remeasurement(self):
        from model.diagnostics.core import MeasurementContext, Trigger
        from model.diagnostics.detectors.ping_pong import PingPongDiagnosis
        from model.issue import Issue
        from tests.support.profile_fixtures import make_incident
        events = [("2026-08-03T08:00:00", "state", None, "1"), ("2026-08-03T08:00:00", "assignment_group", "", "A"),
                  ("2026-08-03T09:00:00", "assignment_group", "A", "G"), ("2026-08-03T10:00:00", "assignment_group", "G", "B"),
                  ("2026-08-03T11:00:00", "assignment_group", "B", "G")]
        ctx = MeasurementContext(Trigger.MANUAL, datetime(2026, 8, 4), [make_incident("I1", events)], [],
                                 thresholds={"moderate_moves": 3, "extreme_moves": 5})
        measurement = next(m for m in PingPongDiagnosis().run(ctx).measurements if m.subject == "I1")
        issue = Issue.from_measurement(measurement)
        issue.details["lesson"] = {"text": "valódi hiba"}
        issue.details["follow_up"] = {"status": "running"}
        issue.apply_measurement(measurement)
        self.assertEqual(issue.details["lesson"], {"text": "valódi hiba"})
        self.assertEqual(issue.details["follow_up"], {"status": "running"})
        self.assertTrue(issue.details["measured_exceeded"])


if __name__ == "__main__":
    unittest.main()
