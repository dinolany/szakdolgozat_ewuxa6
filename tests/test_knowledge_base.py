import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from issue_manager import IssueManager
from models.diagnostics.core import Trigger
from models.diagnostics.feature_usage import FeatureUsageDiagnosis
from models.diagnostics.registry import DiagnosticRegistry
from models.discovery.manager import DiscoveryManager
from models.knowledge_base import KnowledgeBase
from tests.test_feature_usage import AS_OF, incidents


class KnowledgeBaseTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "knowledge.json"

    def tearDown(self):
        self.directory.cleanup()

    def test_facts_persist_and_are_shared(self):
        base = KnowledgeBase(path=str(self.path))
        base.add_fact("szint", {"csoport": "G1", "szint": "L1"}, "2026-08-10")
        # egy másik folyamat (pl. a fő elemző) ugyanazt látja
        other = KnowledgeBase(path=str(self.path))
        self.assertEqual(dict(other.view("2026-08-11").levels), {"G1": "L1"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["meta"]["counters"]["FACT"], 1)

    def test_answers_replace_each_other_and_expire(self):
        base = KnowledgeBase(path=str(self.path))
        base.add_fact("elerheto", {"funkcio": "work_notes", "csoport": "A"})
        base.add_fact("nem_elerheto", {"funkcio": "work_notes", "csoport": "A"})
        self.assertEqual(dict(base.view().availability), {("work_notes", "A"): False})
        base.add_fact("szint", {"csoport": "A", "szint": "L1"}, valid_until="2026-08-31")
        base.add_fact("szint", {"csoport": "A", "szint": "L2"})              # az új besorolás felváltja
        self.assertEqual(dict(base.view("2026-08-15").levels), {"A": "L2"})
        fact, created = base.add_fact("szint", {"csoport": "A", "szint": "L2"})
        self.assertFalse(created)

    def test_discovery_manager_writes_into_the_shared_base(self):
        base = KnowledgeBase(path=str(self.path))
        manager = DiscoveryManager({}, knowledge=base)
        manager.add_fact("nem_dolgozik", {"csoport": "G", "nap": "Sun"})
        self.assertNotIn("facts", manager.state)                             # nem a feltárási állapotban
        self.assertEqual([f.predicate for f in KnowledgeBase(path=str(self.path)).facts()], ["nem_dolgozik"])
        self.assertEqual(len(manager.rules()), 1)                            # a tudásszabály a feltárásé

    def test_import_keeps_ids_and_counter(self):
        base = KnowledgeBase(path=str(self.path))
        moved = base.import_facts({"FACT-0007": {"fact_id": "FACT-0007", "predicate": "szint",
                                                 "args": {"csoport": "G", "szint": "L1"}}})
        self.assertEqual(moved, 1)
        fact, _ = base.add_fact("szint", {"csoport": "H", "szint": "L1"})
        self.assertEqual(fact.fact_id, "FACT-0008")

    def test_registry_passes_knowledge_to_diagnostics(self):
        base = KnowledgeBase(path=str(self.path))
        base.add_fact("elerheto", {"funkcio": "hold_reason", "csoport": "A"})
        db = {"issues": {}, "issue_history": []}
        registry = DiagnosticRegistry([FeatureUsageDiagnosis()])
        registry.execute(db, IssueManager(), Trigger.MANUAL, AS_OF, incidents(), {}, knowledge=base.view(AS_OF.isoformat()))
        hold = next(issue for issue in db["issues"].values() if issue.subject == "hold_reason|A")
        self.assertEqual(hold.details["availability"], "confirmed")


if __name__ == "__main__":
    unittest.main()
