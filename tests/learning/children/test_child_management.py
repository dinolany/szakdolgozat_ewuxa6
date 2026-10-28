"""Tesztek: az almérések kezelése a DiscoveryManagerben (indítók, szerepek, automatikus döntés)."""
from datetime import datetime, timedelta
import unittest

from learning.children.child_def import ChildDef, trigger_matches, trigger_options
from learning.discovery_manager import DiscoveryManager
from learning.state.candidate_state import CandidateState
from learning.state.verdict import Verdict
from tests.support.child_fixtures import AS_OF, IMMEDIATE, START, quick_close, signal
from tests.support.manager_fixtures import DAY, cid, ingest, result
from tests.support.profile_fixtures import make_incident


class TriggerAndManagerTest(unittest.TestCase):
    SAT = (("assignment_group", "G"), ("weekday", "Sat"))

    def setUp(self):
        self.manager = DiscoveryManager()
        ingest(self.manager, [result(self.SAT, ["I1", "I2", "I3"])], DAY)
        self.candidate = self.manager.candidate(cid(self.SAT))

    def test_trigger_matching_and_options(self):
        self.assertTrue(trigger_matches({"items": [["weekday", "Sat"]]}, self.candidate))
        self.assertFalse(trigger_matches({"items": [["weekday", "Sun"]]}, self.candidate))
        self.assertTrue(trigger_matches({"kind": "contrast", "measure": "pickup_delay"}, self.candidate))
        self.assertFalse(trigger_matches({"kind": "signal"}, self.candidate))
        self.assertTrue(trigger_matches({}, self.candidate))
        labels = [o["label"] for o in trigger_options(self.candidate)]
        self.assertEqual(labels[0], f"csak a(z) {self.candidate.candidate_id} jelöltnél")
        self.assertIn("kiugró mintáknál, ahol weekday=Sat", labels)
        self.assertEqual(labels[-1], "minden jelöltnél")

    def test_results_drive_suggestion_and_learning(self):
        child = self.manager.add_child("Azonnali zárás", IMMEDIATE, "explain", 0.6, {"items": [["weekday", "Sat"]]})
        self.assertEqual([c.child_id for c in self.manager.matching_children(self.candidate)], [child.child_id])
        fired = {"child_id": child.child_id, "name": child.name, "role": "explain", "triggered": True, "text": "Azonnali zárás: 80%"}
        self.manager.store_child_results(self.candidate.candidate_id, [fired])
        self.manager.refresh_suggestions()
        suggestion = self.manager.candidate(self.candidate.candidate_id).suggestion
        self.assertEqual((suggestion["verdict"], suggestion["source"]), (Verdict.NOT_FINDING.value, "child"))
        self.manager.decide(self.candidate.candidate_id, Verdict.NOT_FINDING.value, "this_occurrence")   # nem finding: egyezik a magyarázattal
        stats = self.manager.children()[0].stats
        self.assertEqual((stats["runs"], stats["triggered"], stats["decided"], stats["agreed"]), (1, 1, 1, 1))

    def test_children_survive_state_reset_and_revoke(self):
        child = self.manager.add_child("Riasztás", "ujranyitva", "alert")
        fresh = DiscoveryManager({}, knowledge=self.manager.knowledge)    # reset: a feltárási állapot új
        self.assertEqual([c.child_id for c in fresh.children()], [child.child_id])
        self.manager.revoke_child(child.child_id)
        self.assertEqual(self.manager.children(active_only=True), [])
        with self.assertRaises(ValueError):
            self.manager.add_child("Hibás", "nincs_ilyen > 1")

class DimensionAndReviewTest(unittest.TestCase):
    def test_grouping_child_as_search_dimension_uses_its_value(self):
        from learning.mining.profiles import build_profiles
        from storage.knowledge_base import KnowledgeBase
        knowledge = KnowledgeBase()
        manager = DiscoveryManager({}, knowledge=knowledge)
        child = manager.add_child("Ráadó csoport", "elozo_csoport", method="group", as_dimension=True)
        timeline = quick_close("I1", 10)   # L1 -> G
        profiles = build_profiles([make_incident("I1", [((START + timedelta(minutes=m)).isoformat(), f, o, n) for m, f, o, n in (
            (0, "state", None, "1"), (0, "assignment_group", "", "L1"), (5, "assignment_group", "L1", "G"))])],
            AS_OF, START - timedelta(days=1), {}, knowledge.view().derived)
        by_group = {p.dimensions.get("assignment_group"): p.dimensions.get(child.dimension) for p in profiles.stints}
        self.assertEqual(by_group, {"L1": None, "G": "L1"})   # a nyitó szakasznak nincs ráadója: nincs értéke
        self.assertIsNotNone(timeline)

    def test_role_can_be_changed_and_alert_cannot_decide(self):
        manager = DiscoveryManager()
        child = manager.add_child("Gyors", "szakasz_perc < 30", auto=True)
        changed = manager.set_child_role(child.child_id, "alert")
        self.assertEqual((changed.role, changed.auto), ("alert", False))

    def test_child_as_search_dimension(self):
        from learning.mining.profiles import build_profiles
        from storage.knowledge_base import KnowledgeBase
        knowledge = KnowledgeBase()
        manager = DiscoveryManager({}, knowledge=knowledge)
        child = manager.add_child("Close notes-szal zárta", 'lezarta and kitoltve("close_notes")', as_dimension=True)
        other = manager.add_child("Nem szempont", "ujranyitva")
        derived = knowledge.view().derived
        self.assertEqual(derived, ((child.dimension, child.expression, "check"),))
        t = lambda m: (START + timedelta(minutes=m)).isoformat()
        incidents = [make_incident("I1", [(t(0), "state", None, "1"), (t(0), "assignment_group", "", "G"),
                                          (t(9), "close_notes", "", "kész"), (t(9), "state", "1", "6")]),
                     make_incident("I2", [(t(0), "state", None, "1"), (t(0), "assignment_group", "", "G")])]
        profiles = build_profiles(incidents, AS_OF, START - timedelta(days=1), {}, derived)
        values = {p.incident_id: p.dimensions[child.dimension] for p in profiles.stints}
        self.assertEqual(values, {"I1": "igen", "I2": "nem"})
        self.assertNotIn(other.dimension, profiles.tickets[0].dimensions)
        manager.set_child_dimension(child.child_id, False)
        self.assertEqual(knowledge.view().derived, ())

    def test_mining_splits_by_child_dimension(self):
        import random
        from learning import MiningParams, mine
        from tests.support.profile_fixtures import PICKUP, make_profile
        rng = random.Random(3)
        profiles = []
        for index in range(1200):
            opened = START + timedelta(days=rng.randrange(28), hours=rng.randrange(8, 18))
            quick = rng.random() < 0.3
            bad = rng.random() < (0.5 if quick else 0.04)   # a "gyorsan zárt" esetek lassú felvételűek
            profiles.append(make_profile(index, opened, f"G{rng.randrange(1, 6)}", "S1", bad,
                                         {"almeres:CHD-0001": "igen" if quick else "nem"}))
        found = [r.items for r in mine(profiles, PICKUP, MiningParams(min_support=3, fdr_q=0.05))]
        self.assertIn((("almeres:CHD-0001", "igen"),), found)

    def test_review_reasons(self):
        from learning.children.child_review import ChildReviewParams, review_children
        now = datetime(2026, 9, 1)
        base = dict(expression="lezarta", created_at="2026-08-01T00:00:00")
        children = [
            ChildDef("C1", "Félrevisz", stats={"runs": 9, "triggered": 6, "decided": 6, "agreed": 1}, **base),
            ChildDef("C2", "Sosem teljesül", stats={"runs": 12, "triggered": 0, "decided": 0, "agreed": 0}, **base),
            ChildDef("C3", "Sosem fut", stats={"runs": 0, "triggered": 0, "decided": 0, "agreed": 0}, **base),
            ChildDef("C4", "Jó", stats={"runs": 12, "triggered": 6, "decided": 6, "agreed": 6}, **base),
            ChildDef("C5", "Megerősítve", stats={"runs": 12, "triggered": 0, "decided": 0, "agreed": 0},
                     reviewed_at="2026-08-30T00:00:00", reviewed_stats={"runs": 12}, **base),
        ]
        reasons = {i["object_id"]: i["reason"] for i in review_children(children, now, ChildReviewParams())}
        self.assertEqual(reasons, {"C1": "disagreed", "C2": "never_triggered", "C3": "never_ran"})

class ReviewConfigTest(unittest.TestCase):
    def test_review_params_ignore_child_keys(self):
        import tempfile
        from pathlib import Path
        from learning.discovery_config import DiscoveryConfig
        with tempfile.TemporaryDirectory() as tmp:
            config = DiscoveryConfig(str(Path(tmp) / "discovery.json"))
            self.assertEqual(config.review_params().min_contradictions, 3)
            self.assertEqual(config.child_review_params().min_runs, 10)

class ChildAttachTest(unittest.TestCase):
    def test_existing_child_gets_extra_trigger(self):
        manager = DiscoveryManager()
        sat = (("assignment_group", "G"), ("weekday", "Sat"))
        ingest(manager, [result(sat, ["I1", "I2", "I3"])], DAY)
        candidate = manager.candidate(cid(sat))
        child = manager.add_child("Azonnali zárás", IMMEDIATE, trigger={"kind": "signal", "items": [["feature", "work_notes"]]})
        self.assertEqual(manager.matching_children(candidate), [])
        attached, added = manager.attach_child(child.child_id, {"items": [["weekday", "Sat"]]})
        self.assertTrue(added)
        self.assertEqual([c.child_id for c in manager.matching_children(candidate)], [child.child_id])
        self.assertFalse(manager.attach_child(child.child_id, {"items": [["weekday", "Sat"]]})[1])   # nincs duplikátum
        self.assertEqual(len(manager.children()), 1)
        self.assertIn("ahol weekday=Sat", attached.triggers_label)

class SignalNotNowTest(unittest.TestCase):
    def test_not_now_signal_returns_after_the_return_days_if_still_signalling(self):
        manager = DiscoveryManager(signal_return_days=7)
        cid = manager.ingest_signals([signal(["I1", "I2"])], DAY)[0]
        manager.decide(cid, Verdict.NOT_FINDING.value, "this_occurrence")
        manager.ingest_signals([signal(["I1", "I2"])], DAY + timedelta(days=3))
        self.assertEqual(manager.candidate(cid).state, CandidateState.REJECTED_ONCE.value)
        manager.ingest_signals([signal(["I1", "I2"])], DAY + timedelta(days=8))
        self.assertEqual((manager.candidate(cid).state, manager.candidate(cid).flags), (CandidateState.QUEUED.value, ["returned"]))

class AutoDecideTest(unittest.TestCase):
    def test_auto_explanation_decides_without_capacity_or_learning(self):
        manager = DiscoveryManager()
        cid = manager.ingest_signals([signal(["I1", "I2"])], DAY)[0]
        with self.assertRaises(ValueError):
            manager.add_child("Riasztás", "lezarta", role="alert", auto=True)
        child = manager.add_child("Close notes-szal zárta", "lezarta", auto=True)
        manager.store_child_results(cid, [{"child_id": child.child_id, "name": child.name, "role": "explain",
                                           "triggered": True, "text": "90%", "evaluated_at": "x"}])
        self.assertEqual(manager.auto_decide_by_children(), [cid])
        decision = manager.decisions()[-1]
        self.assertEqual((decision.verdict, decision.scope, decision.auto), (Verdict.NOT_FINDING.value, "this_occurrence", child.child_id))
        self.assertEqual(manager.candidate(cid).state, CandidateState.REJECTED_ONCE.value)
        capacity = manager.capacity()
        self.assertEqual((capacity["used_today"], capacity["auto_today"], capacity["learning_decisions"]), (0, 1, 0))
        self.assertEqual(manager._labelled(), [])
        self.assertEqual(manager.children()[0].stats.get("decided", 0), 0)   # önmagát nem igazolja

    def test_previous_share_is_kept(self):
        manager = DiscoveryManager()
        cid = manager.ingest_signals([signal(["I1"])], DAY)[0]
        first = {"child_id": "C", "name": "x", "role": "explain", "triggered": False, "share": 0.2, "evaluated_at": "t1"}
        manager.store_child_results(cid, [first])
        manager.store_child_results(cid, [dict(first, share=0.7, evaluated_at="t2")])
        result = manager.candidate(cid).stats["children"][0]
        self.assertEqual((result["share"], result["previous_share"], result["previous_at"]), (0.7, 0.2, "t1"))


if __name__ == "__main__":
    unittest.main()
