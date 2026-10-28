from types import SimpleNamespace
import unittest

from engine.issue_manager import IssueManager
from learning.discovery_manager import DiscoveryManager
from learning.discovery_service import DiscoveryService
from learning.state.candidate_state import CandidateState
from learning.state.verdict import Verdict
from model.issue import Issue
from model.issue_state import IssueState
from storage.knowledge_base import KnowledgeBase
from tests.test_knowledge import DAY, cid, ingest, result


G_SAT = (("assignment_group", "G"), ("weekday", "Sat"))
G = (("assignment_group", "G"),)


class AttachTest(unittest.TestCase):
    def test_narrower_candidate_attaches_to_open_issue_and_returns_when_closed(self):
        manager = DiscoveryManager()
        ingest(manager, [result(G, ["I1", "I2", "I3", "I4"]), result(G_SAT, ["I1", "I2", "I3"])], DAY)
        manager.decide(cid(G), Verdict.FINDING.value)
        manager.link_issue(cid(G), "ISSUE-1")
        attached = manager.attach_to_open_issues({"ISSUE-1"})
        self.assertEqual(attached, [(cid(G_SAT), "ISSUE-1")])
        self.assertEqual(manager.candidate(cid(G_SAT)).state, CandidateState.ATTACHED.value)
        self.assertNotIn(cid(G_SAT), [c.candidate_id for c in manager._open_pool()])     # nem kerül a sorba
        self.assertEqual(manager.release_attached({"ISSUE-1"}), [])                       # amíg nyitott, csatolva marad
        self.assertEqual(manager.release_attached(set()), [cid(G_SAT)])                   # lezárva: újra döntésre vár
        self.assertEqual(manager.candidate(cid(G_SAT)).state, CandidateState.QUEUED.value)


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


class MethodFeedbackTest(unittest.TestCase):
    def test_good_method_generalizes_bad_excludes(self):
        manager = DiscoveryManager()
        ingest(manager, [result(G, ["I1", "I2", "I3"]), result(G_SAT, ["I1", "I2"])], DAY)
        child = manager.add_child("Gyors átadás", "tovabbitotta", trigger={"candidate_id": cid(G)})
        self.assertFalse(child.matches(manager.candidate(cid(G_SAT))))
        manager.decide(cid(G), Verdict.REFINE.value)
        manager.child_method_feedback(child.child_id, cid(G), "good")
        self.assertTrue(manager._child(child.child_id).matches(manager.candidate(cid(G_SAT))))   # minden ilyen jelöltnél
        manager.child_method_feedback(child.child_id, cid(G), "bad")
        self.assertFalse(manager._child(child.child_id).matches(manager.candidate(cid(G))))       # ennél a fajtánál többé nem


if __name__ == "__main__":
    unittest.main()


class ProbeFeedbackTest(unittest.TestCase):
    def test_factory_probe_good_becomes_child_bad_is_excluded(self):
        manager = DiscoveryManager()
        ingest(manager, [result(G, ["I1", "I2", "I3"])], DAY)
        manager.decide(cid(G), Verdict.REFINE.value)
        probe = {"probe": "gyors_tovabbitas", "source": "gyári", "name": "Rögtön továbbadta", "expression": "tovabbitotta and szakasz_perc < 30"}
        manager.candidate(cid(G)).stats["probes"] = [probe]
        child = manager.probe_feedback(cid(G), probe, "good")
        self.assertTrue(child.matches(manager.candidate(cid(G))))
        self.assertEqual(manager.candidate(cid(G)).stats["probes"], [])
        manager.probe_feedback(cid(G), {**probe, "probe": "meg_nyitott"}, "bad")
        self.assertEqual(manager.excluded_probes(manager.candidate(cid(G))), {"meg_nyitott"})


class FieldRuleTest(unittest.TestCase):
    def test_wildcard_rule_drops_every_value_of_the_field(self):
        manager = DiscoveryManager()
        sub1 = (("assignment_group", "G"), ("subcategory", "1"))
        sub2 = (("subcategory", "7"),)
        ingest(manager, [result(sub1, ["I1", "I2", "I3"]), result(sub2, ["I4", "I5", "I6"]), result(G, ["I7", "I8", "I9"])], DAY)
        rule = manager.add_rule([{"dim": "subcategory", "values": ["*"]}])
        self.assertEqual({c.candidate_id for c in manager.queue()}, {cid(G)})
        self.assertIn("bármely érték", rule.render())

    def test_hard_rule_hides_even_extreme_candidate(self):
        manager = DiscoveryManager()
        manager.extreme_strength = 0.0   # minden jelölt "áttörő": a sima elvetés nem rejti
        ingest(manager, [result(G, ["I1", "I2", "I3"])], DAY)
        manager.add_rule([{"dim": "assignment_group", "values": ["G"]}])
        self.assertEqual([c.candidate_id for c in manager.queue()], [cid(G)])
        manager.add_rule([{"dim": "assignment_group", "values": ["G"]}], hard=True)
        self.assertEqual(manager.queue(), [])
