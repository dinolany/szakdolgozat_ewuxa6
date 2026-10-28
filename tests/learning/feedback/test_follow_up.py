"""Tesztek: learning/feedback/follow_up.py."""
from datetime import datetime
import unittest

from learning.feedback import follow_up as fu
from tests.support.follow_up_fixtures import NOW


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


if __name__ == "__main__":
    unittest.main()
