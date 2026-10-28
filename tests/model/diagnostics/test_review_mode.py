"""Tesztek: a detektorok jóváhagyásos módja (model/diagnostics/core)."""
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from engine.issue_manager import IssueManager
from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.detectors.feature_usage.feature_usage_diagnosis import FeatureUsageDiagnosis
from model.diagnostics.diagnostic_registry import DiagnosticRegistry
from model.knowledge.knowledge_view import KnowledgeView
from storage.threshold_store import ThresholdManager
from tests.model.diagnostics.test_feature_usage import AS_OF, incidents


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


if __name__ == "__main__":
    unittest.main()
