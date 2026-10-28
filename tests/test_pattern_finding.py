from datetime import datetime, timedelta
import unittest

from engine.issue_manager import IssueManager
from learning.discovery_manager import DiscoveryManager
from learning.pattern_finding_diagnosis import PatternFindingDiagnosis
from learning.promotion import promote_finding
from learning.state.candidate import Candidate, make_candidate_id
from learning.state.candidate_state import CandidateState
from learning.state.verdict import Verdict
from model.diagnostics.core import MeasurementContext, Trigger
from model.issue import Issue
from model.issue_state import IssueState
from tests.test_discovery import make_incident
from tests.test_knowledge import ingest, result


START = datetime(2026, 8, 3)  # hétfő


def week_of_incidents():
    """Egy hét: a G csoport vasárnapi ticketjeit 12 óra múlva veszik fel, minden mást 1 óra alatt."""
    incidents = []
    for index in range(42):
        day = index % 7
        group = "G" if index % 2 == 0 else "H"
        opened = START + timedelta(days=day, hours=9 + index % 5)
        slow = group == "G" and day == 6
        assigned = opened + timedelta(hours=12 if slow else 1)
        incidents.append(make_incident(f"INC{index:03d}", [
            (opened.isoformat(), "state", None, "5"),
            (opened.isoformat(), "assignment_group", "", group),
            (assigned.isoformat(), "assigned_to", "", "U1"),
        ]))
    return incidents


def candidate(key, measure="pickup_delay"):
    return Candidate(make_candidate_id(measure, key), measure, key, state=CandidateState.ACCEPTED.value)


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


class CoverageTest(unittest.TestCase):
    def test_candidates_covered_by_accepted_finding_are_ranked_lower(self):
        manager = DiscoveryManager()
        service = (("business_service", "S"),)
        narrowed = (("business_service", "S"), ("weekday", "Thu"))
        unrelated = (("business_service", "T"), ("weekday", "Thu"))
        day = datetime(2026, 8, 10)
        ingest(manager, [result(service, ["I1", "I2", "I3", "I4"])], day)
        manager.decide(make_candidate_id("pickup_delay", "business_service=S"), Verdict.FINDING.value)

        ingest(manager, [result(narrowed, ["I1", "I2", "I5"]), result(unrelated, ["J1", "J2", "J3"])],
               day + timedelta(days=1))
        covered = manager.candidate(make_candidate_id("pickup_delay", "business_service=S&weekday=Thu"))
        other = manager.candidate(make_candidate_id("pickup_delay", "business_service=T&weekday=Thu"))
        self.assertEqual(covered.covered_by, "business_service=S")
        self.assertEqual(covered.state, CandidateState.QUEUED.value)  # nem rejtett, csak hátrébb sorolt
        self.assertIsNone(other.covered_by)
        self.assertLess(covered.priority, other.priority)


if __name__ == "__main__":
    unittest.main()
