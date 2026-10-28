"""Tesztek: learning/feedback/lessons.py."""
import unittest

from learning.feedback.lessons import lesson_options
from learning.state.candidate import Candidate
from model.knowledge.predicates import PREDICATES


class LessonOptionsTest(unittest.TestCase):
    def test_pattern_finding_offers_known_deviation_and_prefills(self):
        candidate = Candidate("C1", "stint_pickup", "assignment_group=G&weekday=Sat")
        options = lesson_options({"issue_id": "I1", "details": {}}, candidate, (), "PATTERN_FINDING")
        keys = [o["key"] for o in options["outcomes"]]
        self.assertEqual(keys, ["confirmed", "off_hours", "false_alarm", "known_deviation", "none"])
        self.assertEqual((options["defaults"]["groups"], options["defaults"]["days"]), (["G"], ["Sat"]))

    def test_feature_signal_offers_not_expected(self):
        candidate = Candidate("C2", "FEATURE_USAGE", "assignment_group=G&feature=work_notes", kind="signal")
        options = lesson_options({"issue_id": "I2", "details": {"feature": "work_notes", "group": "G"}}, candidate, (), "FEATURE_USAGE")
        self.assertIn("not_expected", [o["key"] for o in options["outcomes"]])
        self.assertNotIn("known_deviation", [o["key"] for o in options["outcomes"]])
        self.assertEqual(options["defaults"]["feature"], "work_notes")

    def test_diagnostic_issue_rule_items_skip_the_ticket(self):
        items = (("incident", "INC1"), ("assignment_group", "A"), ("assignment_group", "B"))
        options = lesson_options({"issue_id": "I3", "details": {}, "groups": ["A", "B"]}, None, items, "PING_PONG")
        self.assertEqual(options["defaults"]["rule_items"], [["assignment_group", "A"], ["assignment_group", "B"]])
        self.assertIsNone(options["candidate_id"])

    def test_not_expected_is_a_knowledge_rule_predicate(self):
        self.assertEqual(PREDICATES["nem_elvart"].kind, "drop")
        self.assertEqual(dict(PREDICATES["nem_elvart"].args), {"funkcio": "feature", "csoport": "assignment_group"})


if __name__ == "__main__":
    unittest.main()
