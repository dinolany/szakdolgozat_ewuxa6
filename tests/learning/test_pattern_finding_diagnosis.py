"""Tesztek: learning/pattern_finding_diagnosis.py és learning/promotion.py."""
from datetime import datetime, timedelta
import unittest

from engine.issue_manager import IssueManager
from learning.pattern_finding_diagnosis import PatternFindingDiagnosis
from learning.promotion import promote_finding
from learning.state.candidate import Candidate, make_candidate_id
from learning.state.candidate_state import CandidateState
from model.diagnostics.core import MeasurementContext, Trigger
from model.issue import Issue
from model.issue_state import IssueState
from model.knowledge.knowledge_view import KnowledgeView
from tests.support.finding_fixtures import START, candidate, week_of_incidents
from tests.support.stint_fixtures import LEVELS, week


class PatternFindingTest(unittest.TestCase):
    def setUp(self):
        self.incidents = week_of_incidents()
        self.db = {"issues": {}, "issue_history": []}
        self.manager = IssueManager()
        self.diagnostic = PatternFindingDiagnosis()

    def ctx(self, day: datetime, trigger=Trigger.EVENT_BATCH):
        issues = [i if isinstance(i, Issue) else Issue.from_dict(i) for i in self.db["issues"].values()]
        return MeasurementContext(trigger, day, self.incidents, issues)

    def promote(self, key, day):
        return promote_finding(self.db, self.manager, self.diagnostic, candidate(key), self.ctx(day, Trigger.MANUAL), 8)

    def test_finding_becomes_active_issue_with_definition(self):
        issue = self.promote("assignment_group=G&weekday=Sun", START + timedelta(days=6, hours=23))
        self.assertEqual(issue.state, IssueState.ACTIVE)
        self.assertEqual(issue.subject, "pickup_delay|assignment_group=G&weekday=Sun")
        self.assertEqual(issue.details["pattern"], [["assignment_group", "G"], ["weekday", "Sun"]])
        self.assertEqual(issue.trigger, Trigger.MANUAL)
        self.assertEqual(issue.window_type, "SLIDING")
        self.assertEqual(len(issue.affected_objects), 3)  # a 3 vasárnapi G ticket
        self.assertTrue(issue.history_refs)               # a felvételi események
        # nagy a kiemelés, de csak 3 rossz ticket: nem extrém
        self.assertEqual(str(issue.severity), "EXCEEDED_MODERATE")

    def test_finding_is_remeasured_and_outdated_when_window_passes(self):
        issue = self.promote("assignment_group=G&weekday=Sun", START + timedelta(days=6, hours=23))
        run = self.diagnostic.run(self.ctx(START + timedelta(days=8)))
        self.manager.apply_run(self.db, run, "diagnostic_rerun")
        self.assertEqual(self.db["issues"][issue.issue_id].state, IssueState.ACTIVE)
        # két héttel később nincs a mintára illő ticket a 7 napos ablakban: a minta elmúlt
        run = self.diagnostic.run(self.ctx(START + timedelta(days=21)))
        self.manager.apply_run(self.db, run, "diagnostic_rerun")
        self.assertEqual(self.db["issues"][issue.issue_id].state, IssueState.OUTDATED)

    def test_accepted_but_not_outstanding_pattern_is_created_not_relevant(self):
        issue = self.promote("assignment_group=H", START + timedelta(days=6, hours=23))
        self.assertEqual(issue.state, IssueState.NOT_RELEVANT)
        self.assertEqual(self.db["issue_history"][-1]["reason"], "finding_accepted_below_threshold")

    def test_no_issue_without_matching_tickets(self):
        self.assertIsNone(self.promote("assignment_group=X", START + timedelta(days=6, hours=23)))
        self.assertEqual(self.db["issues"], {})

class StintFindingTest(unittest.TestCase):
    def test_stint_finding_is_measured_on_stints(self):
        incidents = week()
        diagnostic = PatternFindingDiagnosis()
        db = {"issues": {}, "issue_history": []}
        key = "assignment_group=A&weekday=Sat"
        ctx = MeasurementContext(Trigger.MANUAL, START + timedelta(days=12, hours=23), incidents, [],
                                 knowledge=KnowledgeView(levels=LEVELS))
        found = Candidate(make_candidate_id("stint_pickup", key), "stint_pickup", key, state=CandidateState.ACCEPTED.value)
        issue = promote_finding(db, IssueManager(), diagnostic, found, ctx, 8)
        self.assertEqual(issue.details["population"], 70)       # egy hét rezolver szakaszai
        self.assertEqual(issue.details["bad_count"], 5)          # az A csoport szombati érkezései
        self.assertEqual(len(issue.affected_objects), 5)


if __name__ == "__main__":
    unittest.main()
