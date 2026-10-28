"""A tanuló modul megbízhatósága: mentés és visszaállítás, a szabályok túlélik a resetet, biztonsági szelep, mérleg."""
from pathlib import Path
import tempfile
import unittest

from engine.analyzer import IncidentAnalyzer
from learning.discovery_service import DiscoveryService
from learning.state.candidate_state import CandidateState
from learning.state.scope import Scope
from learning.state.verdict import Verdict
from storage import backup
from storage.incident_repository import IncidentRepository
from tests.test_knowledge import DAY, cid, ingest, result


G = (("assignment_group", "G"),)


class BackupTest(unittest.TestCase):
    def test_snapshot_throttles_restores_and_keeps_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = base / "a.json"
            a.write_text("1", encoding="utf-8")
            first = backup.snapshot([a], base / "backups")
            self.assertIsNotNone(first)
            self.assertIsNone(backup.snapshot([a], base / "backups"))               # túl friss: nincs új
            a.write_text("2", encoding="utf-8")
            backup.restore(base / "backups", first, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "1")
            names = [s["name"] for s in backup.list_snapshots(base / "backups")]
            self.assertTrue(any(n.startswith("napi-") for n in names))
            with self.assertRaises(ValueError):
                backup.restore(base / "backups", "../a.json", [a])                 # csak a saját mentései közül


class ServiceSafetyTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / "config").mkdir()
        self.repo = IncidentRepository(str(base / "database.json"))
        self.analyzer = IncidentAnalyzer(str(base / "thresholds.json"), knowledge_path=str(base / "knowledge.json"))
        self.service = DiscoveryService(self.analyzer, self.repo, str(base / "discovery_state.json"), str(base / "config" / "discovery.json"))

    def tearDown(self):
        self.directory.cleanup()

    def test_rules_survive_reset(self):
        manager = self.service.manager
        ingest(manager, [result(G, ["I1", "I2", "I3"])], DAY)
        rule = manager.add_rule([{"dim": "assignment_group", "values": ["G"]}], hard=True)
        self.service._save()   # a valódi alkalmazásban a fájlok már léteznek
        self.service.reset()
        self.assertIn(rule.rule_id, self.service.manager.state["rules"])
        self.assertEqual(self.service.manager.candidates(), [])
        self.assertNotEqual(self.service.manager._next_id("RULE"), rule.rule_id)   # az azonosítók nem ütköznek
        self.assertTrue(any("reset-elott" in s["name"] for s in self.service.backup_overview()["snapshots"]))

    def test_restore_brings_back_the_earlier_knowledge(self):
        self.service.knowledge.add_fact("szint", {"csoport": "L1G", "szint": "L1"})
        self.service.knowledge.save()
        name = self.service.backup_action({"op": "create"})["name"]
        self.service.knowledge.add_fact("szint", {"csoport": "MAS", "szint": "L2"})
        self.service.knowledge.save()
        self.service.backup_action({"op": "restore", "name": name})
        self.assertEqual(self.service.knowledge.view().levels, {"L1G": "L1"})

    def test_strong_candidate_under_a_rule_comes_to_review(self):
        manager = self.service.manager
        manager.extreme_strength = 0.0   # minden jelölt "nagyon erős"
        ingest(manager, [result(G, ["I1", "I2", "I3"])], DAY)
        rule = manager.add_rule([{"dim": "assignment_group", "values": ["G"]}], hard=True)
        ingest(manager, [result(G, ["I1", "I2", "I3", "I4"])], DAY.replace(day=DAY.day + 1))   # a szabály után is megjelent
        self.assertEqual(manager.candidate(cid(G)).state, CandidateState.FILTERED.value)
        items = self.service._strong_under_rules({})
        self.assertEqual([(i["object_id"], i["reason"], i["candidate_id"]) for i in items], [(rule.rule_id, "strong_under_rule", cid(G))])

    def test_learning_balance_counts_decisions_per_week(self):
        manager = self.service.manager
        ingest(manager, [result(G, ["I1", "I2", "I3"])], DAY)
        manager.decide(cid(G), Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value)
        week = self.service.learning_balance()[-1]
        self.assertEqual((week["new"], week["decisions"], week["not_now"], week["finding"]), (1, 1, 1, 0))


if __name__ == "__main__":
    unittest.main()
