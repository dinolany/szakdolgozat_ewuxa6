import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from analyzer import IncidentAnalyzer
from config.thresholds import ThresholdManager
from issue_manager import IssueManager
from models.diagnostics.core import MeasurementContext, Trigger
from models.diagnostics.feature_usage import FeatureUsageDiagnosis
from models.diagnostics.registry import DiagnosticRegistry
from models.discovery.knowledge import CandidateState
from models.discovery.service import DiscoveryService
from models.knowledge_base import KnowledgeView
from repository import IncidentRepository
from tests.test_feature_usage import AS_OF, incidents


class ReviewModeFrameworkTest(unittest.TestCase):
    def test_review_mode_opens_no_new_issue_but_keeps_measuring_tasks(self):
        db = {"issues": {}, "issue_history": []}
        registry = DiagnosticRegistry([FeatureUsageDiagnosis()])
        knowledge = KnowledgeView(availability={("hold_reason", "A"): True})
        registry.execute(db, IssueManager(), Trigger.MANUAL, AS_OF, incidents(), {}, knowledge=knowledge, review={"feature_usage"})
        self.assertEqual(db["issues"], {})
        # elfogadott jelzés -> feladat; ezután jóváhagyásos módban is mérődik
        diagnostic = registry.get("FEATURE_USAGE")
        ctx = MeasurementContext(Trigger.MANUAL, AS_OF, incidents(), [], knowledge=knowledge)
        IssueManager().apply_run(db, diagnostic.accept_signal(ctx, "hold_reason|A"), "signal_accepted")
        self.assertEqual([i.subject for i in db["issues"].values()], ["hold_reason|A"])
        later = AS_OF + timedelta(days=60)                     # az ablakból kiesett: nincs alkalom
        registry.execute(db, IssueManager(), Trigger.MANUAL, later, incidents(), {}, knowledge=knowledge, review={"feature_usage"})
        [task] = db["issues"].values()
        self.assertEqual(task.state.value, "outdated")
        self.assertEqual(len(db["issues"]), 1)

    def test_review_setting_defaults_and_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "thresholds.json")
            manager = ThresholdManager([FeatureUsageDiagnosis()], storage_path=path)
            self.assertEqual(manager.review_keys, frozenset({"feature_usage"}))
            manager.set_review("feature_usage", False)
            self.assertEqual(ThresholdManager([FeatureUsageDiagnosis()], storage_path=path).review_keys, frozenset())


class SignalLoopTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / "config").mkdir()
        self.repo = IncidentRepository(str(base / "database.json"))
        with self.repo.transaction() as db:
            for incident in incidents():
                db["incidents"][incident.id] = incident
        self.analyzer = IncidentAnalyzer(str(base / "thresholds.json"), knowledge_path=str(base / "knowledge.json"))
        self.service = DiscoveryService(self.analyzer, self.repo, str(base / "state.json"), str(base / "config" / "discovery.json"))
        self.service.run(AS_OF)

    def tearDown(self):
        self.directory.cleanup()

    def signal(self, subject):
        return next(c for c in self.service.manager.candidates() if c.kind == "signal" and c.stats.get("subject") == subject)

    def test_signals_become_questions_or_candidates_not_issues(self):
        overview = self.service.overview(show_all=True)
        self.assertFalse([i for i in self.repo._load_db()["issues"].values() if i.diagnostic_key == "FEATURE_USAGE"])
        questions = {c["stats"]["subject"] for c in overview["questions"]}
        self.assertIn("hold_reason|A", questions)             # sosem használta, elérhetőség ismeretlen: kérdés
        self.assertNotIn(self.signal("hold_reason|A").candidate_id, [c["candidate_id"] for c in overview["queue"]])

    def test_answer_turns_question_into_decision_and_acceptance_into_task(self):
        self.service.availability({"feature": "hold_reason", "group": "A", "available": True})
        candidate = self.signal("hold_reason|A")
        self.assertEqual(candidate.stats["availability"], "confirmed")
        overview = self.service.overview(show_all=True)
        self.assertIn(candidate.candidate_id, [c["candidate_id"] for c in overview["queue"]])
        result = self.service.decide({"candidate_id": candidate.candidate_id, "action": "finding"})
        issues = self.repo._load_db()["issues"]
        self.assertIn(result["issue_id"], issues)
        self.assertEqual(issues[result["issue_id"]].diagnostic_key, "FEATURE_USAGE")
        self.assertEqual(self.signal("hold_reason|A").state, CandidateState.ACCEPTED.value)

    def test_reject_pattern_hides_the_signal_later(self):
        self.service.availability({"feature": "hold_reason", "group": "A", "available": True})
        candidate = self.signal("hold_reason|A")
        self.service.suppress({"op": "pattern", "candidate_id": candidate.candidate_id})   # Ez a minta soha
        self.service.run(AS_OF + timedelta(days=1))
        self.assertEqual(self.signal("hold_reason|A").state, CandidateState.REJECTED.value)


if __name__ == "__main__":
    unittest.main()
