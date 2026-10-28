import unittest
from datetime import datetime

from models.discovery import follow_up as fu

NOW = "2026-08-01T00:00:00"


class FollowUpTest(unittest.TestCase):
    def test_improve_waits_until_due_then_decides(self):
        plan = fu.start("improve", 7, NOW, {"metric_value": 12})
        still_bad = {"details": {"measured_exceeded": True}}
        self.assertEqual(fu.evaluate(plan, still_bad, datetime(2026, 8, 5)), ("running", None))
        self.assertEqual(fu.evaluate(plan, still_bad, datetime(2026, 8, 8)), ("not_improved", "reopen"))
        self.assertEqual(fu.evaluate(plan, {"details": {"measured_exceeded": False}}, datetime(2026, 8, 8)), ("improved", None))

    def test_no_repeat_alerts_immediately_on_matching_new_issue(self):
        plan = fu.start("no_repeat", 14, NOW, {}, [["current_group", "G"]])
        issues = [
            {"issue_id": "OLD", "diagnostic_key": "PING_PONG", "first_detected_at": "2026-07-20T00:00:00"},
            {"issue_id": "OTHER-GROUP", "diagnostic_key": "PING_PONG", "first_detected_at": "2026-08-03T00:00:00", "g": "H"},
            {"issue_id": "NEW", "diagnostic_key": "PING_PONG", "first_detected_at": "2026-08-03T00:00:00", "g": "G"},
            {"issue_id": "OTHER-DIAG", "diagnostic_key": "FLAPPING_INCIDENT", "first_detected_at": "2026-08-03T00:00:00", "g": "G"},
        ]
        entity_of = lambda i: [("current_group", i.get("g", ""))]
        repeats = fu.matching_repeats(plan, "ORIGINAL", "PING_PONG", issues, entity_of)
        self.assertEqual(repeats, ["NEW"])
        self.assertEqual(fu.evaluate(plan, {}, datetime(2026, 8, 4), repeats), ("repeated", None))
        self.assertEqual(fu.evaluate(plan, {}, datetime(2026, 8, 16), []), ("not_repeated", None))
        with self.assertRaises(ValueError):
            fu.start("no_repeat", 14, NOW, {}, [])


class IssueKeepsAnalystDataTest(unittest.TestCase):
    def test_lesson_and_follow_up_survive_remeasurement(self):
        from models.diagnostics.core import MeasurementContext, Trigger
        from models.diagnostics.ping_pong import PingPongDiagnosis
        from models.issue import Issue
        from tests.test_discovery import make_incident
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
