"""Tesztek: learning/discovery_service.py."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from engine.analyzer import IncidentAnalyzer
from engine.event_processor import IncidentEventProcessor
from engine.issue_manager import IssueManager
from learning.discovery_service import DiscoveryService
from learning.state.candidate import CandidateState
from learning.state.decision import Scope, Verdict
from model.issue import Issue
from model.issue_state import IssueState
from storage.incident_repository import IncidentRepository
from storage.knowledge_base import KnowledgeBase
from tests.model.diagnostics.test_feature_usage import AS_OF, incidents
from tests.support.known_fault_fixtures import G
from tests.support.manager_fixtures import DAY, cid, ingest, result


class KnownFaultTest(unittest.TestCase):
    def service(self, issue):
        db = {"issues": {issue.issue_id: issue}, "issue_history": []}
        fake = SimpleNamespace(analyzer=SimpleNamespace(issue_manager=IssueManager()), knowledge=KnowledgeBase())
        check = lambda now: DiscoveryService._check_known_faults(fake, db, now)
        return db, check

    def test_confirmed_issue_reopens_when_fault_returns_after_quiet(self):
        from datetime import datetime
        issue = Issue("ISSUE-1", "PATTERN", None, state=IssueState.RESOLVED, details={"lesson": {"outcome": "confirmed"}, "measured_exceeded": True})
        db, check = self.service(issue)
        self.assertEqual(check(datetime(2026, 8, 1)), [])              # még nem csendesedett el: nem nyílik újra
        issue.details["measured_exceeded"] = False
        self.assertEqual(check(datetime(2026, 8, 2)), [])              # elcsendesedett
        issue.details["measured_exceeded"] = True
        self.assertEqual(check(datetime(2026, 8, 5)), ["ISSUE-1"])     # visszatért: újranyílik
        self.assertEqual(db["issues"]["ISSUE-1"].state, IssueState.ACTIVE)
        self.assertIn("known_fault_returned_at", db["issues"]["ISSUE-1"].details)

    def test_other_lessons_do_not_reopen(self):
        from datetime import datetime
        issue = Issue("ISSUE-2", "PATTERN", None, state=IssueState.RESOLVED, details={"lesson": {"outcome": "false_alarm"}, "measured_exceeded": False})
        db, check = self.service(issue)
        check(datetime(2026, 8, 2))
        issue.details["measured_exceeded"] = True
        self.assertEqual(check(datetime(2026, 8, 5)), [])

def events_for_day(day: datetime, count: int = 6):
    rows = []
    for index in range(count):
        opened = day + timedelta(hours=9, minutes=10 * index)
        incident = f"INC{day:%d}{index:02d}"
        rows += [
            {"set": incident, "timestamp": opened.isoformat(), "field": "state", "old_value": None, "new_value": "1"},
            {"set": incident, "timestamp": opened.isoformat(), "field": "assignment_group", "old_value": "", "new_value": "L1"},
            {"set": incident, "timestamp": (opened + timedelta(minutes=5)).isoformat(), "field": "assignment_group",
             "old_value": "L1", "new_value": "A" if index % 2 else "B"},
        ]
    return rows

class DiscoveryServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / "config").mkdir()
        self.repo = IncidentRepository(str(base / "database.json"))
        self.analyzer = IncidentAnalyzer(str(base / "thresholds.json"), knowledge_path=str(base / "knowledge.json"))
        self.service = DiscoveryService(self.analyzer, self.repo, str(base / "discovery_state.json"),
                                        str(base / "config" / "discovery.json"))
        self.processor = IncidentEventProcessor(self.repo, self.analyzer, discovery=self.service)

    def tearDown(self):
        self.directory.cleanup()

    def last_run(self):
        return (self.service.manager.state["meta"].get("last_run") or {}).get("measured_at")

    def test_runs_once_per_virtual_day_after_batches(self):
        day1, day2 = datetime(2026, 8, 3), datetime(2026, 8, 4)
        rows = events_for_day(day1)
        self.processor.process_events(rows[:9])
        first = self.last_run()
        self.assertIsNotNone(first)
        self.processor.process_events(rows[9:])          # ugyanaz a nap: nem fut újra
        self.assertEqual(self.last_run(), first)
        self.processor.process_events(events_for_day(day2))
        self.assertTrue(self.last_run().startswith("2026-08-04"))

    def test_level_fact_reanalyzes_the_main_database(self):
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)))
        self.service.fact({"op": "add", "predicate": "szint", "args": {"csoport": "L1", "szint": "L1"}})
        self.assertEqual(dict(self.analyzer.knowledge.view().levels), {"L1": "L1"})
        self.assertTrue(Path(self.directory.name, "knowledge.json").exists())

    def test_time_jumping_back_restarts_discovery(self):
        self.processor.process_events(events_for_day(datetime(2026, 8, 10)))
        self.assertTrue(self.last_run().startswith("2026-08-10"))
        self.service.manager.state["meta"]["marker"] = "régi lejátszás"
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)))   # új lejátszás reset nélkül
        self.assertTrue(self.last_run().startswith("2026-08-03"))
        self.assertNotIn("marker", self.service.manager.state["meta"])

    def test_analysis_flags_candidate_not_covered_by_current_data(self):
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)) + events_for_day(datetime(2026, 8, 4)))
        candidate = next(iter(self.service.manager.candidates()), None)
        if candidate is None:
            self.skipTest("a kis adatból nem lett jelölt")
        candidate.window_end = "2026-08-20T00:00:00"                        # egy későbbi mérésből
        self.service.manager._save_candidate(candidate)
        self.assertTrue(self.service.analysis(candidate.candidate_id)["stale"])

    def test_reset_keeps_knowledge(self):
        self.service.fact({"op": "add", "predicate": "szint", "args": {"csoport": "L1", "szint": "L1"}})
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)))
        self.service.reset()
        self.assertIsNone(self.last_run())
        self.assertEqual(len(self.service.knowledge_overview()["facts"]), 1)

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
