"""Tesztek: learning/discovery_manager.py."""
from datetime import datetime, timedelta
import unittest

from learning.discovery_manager import DiscoveryManager
from learning.state.candidate import CandidateState, make_candidate_id
from learning.state.decision import Scope, Verdict
from tests.support.cluster_fixtures import G, G_H10, G_SUN, H_SUN
from tests.support.diagnostic_fixtures import L1
from tests.support.finding_fixtures import candidate
from tests.support.known_fault_fixtures import G_SAT
from tests.support.manager_fixtures import DAY, G_SUN_10, cid, counts, ingest, result
from tests.support.stint_fixtures import week


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

class ManagerClusterTest(unittest.TestCase):
    def setUp(self):
        self.manager = DiscoveryManager()
        ingest(self.manager, [result(G, ["I1", "I2", "I3", "I4"]), result(G_SUN, ["I1", "I2", "I3"]),
                              result(G_H10, ["I2", "I3", "I4"]), result(H_SUN, ["J1", "J2", "J3"])], DAY)

    def test_general_candidate_is_ranked_first_by_reach(self):
        general = self.manager.candidate(cid(G))
        self.assertEqual(sorted(general.reach), sorted([cid(G_SUN), cid(G_H10)]))
        self.assertEqual(self.manager.queue()[0].candidate_id, cid(G))

    def test_cluster_points_to_the_general_candidate(self):
        [cluster] = [c for c in self.manager.clusters() if c["items"] == [("assignment_group", "G")]]
        self.assertEqual(len(cluster["members"]), 3)
        self.assertEqual(cluster["candidate_id"], cid(G))

class CapacityTest(unittest.TestCase):
    def setUp(self):
        self.manager = DiscoveryManager()
        self.manager.training_capacity, self.manager.decision_capacity, self.manager.training_decisions = 3, 2, 4
        ingest(self.manager, [result(G, ["I1", "I2", "I3", "I4"]), result(G_SUN, ["I1", "I2", "I3"]),
                              result(G_H10, ["I2", "I3", "I4"]), result(H_SUN, ["J1", "J2", "J3"]),
                              result((("assignment_group", "K"),), ["K1", "K2", "K3"])], DAY)

    def test_training_then_steady_each_candidate_counts_once(self):
        cap = self.manager.capacity()
        self.assertEqual((cap["phase"], cap["capacity"], cap["remaining"]), ("training", 3, 3))
        for items in (G, G_SUN, G_H10):
            self.manager.decide(cid(items), "not_finding", "this_occurrence")
        cap = self.manager.capacity()
        self.assertEqual((cap["used_today"], cap["learning_decisions"], cap["phase"]), (3, 3, "training"))
        self.manager.decide(cid(H_SUN), "not_finding", "this_occurrence")
        cap = self.manager.capacity()
        self.assertEqual((cap["phase"], cap["capacity"], cap["used_today"], cap["remaining"]), ("steady", 2, 4, 0))

    def test_queue_is_cut_at_the_remaining_decisions(self):
        # minden jelölt egy döntés (a családokon nincs közös művelet)
        self.assertEqual(len(self.manager.queue(20, decisions=3)), 3)
        self.assertEqual(len(self.manager.queue(20, decisions=1)), 1)
        self.assertEqual(self.manager.queue(20, decisions=0), [])

class GoodDimensionTest(unittest.TestCase):
    def test_good_dimension_is_remembered_per_measure_not_per_value(self):
        from tests.support.manager_fixtures import cid, ingest, result
        manager = DiscoveryManager()
        group = (("assignment_group", "G"),)
        ingest(manager, [result(group, ["I1", "I2", "I3"])], DAY)
        before = len(manager.candidates())
        manager.breakdown_feedback(cid(group), "subcategory", "1", "good")
        self.assertEqual(manager.good_dimensions("pickup_delay"), ["subcategory"])   # a mérésnél jó szempont
        self.assertEqual(manager.good_dimensions("stint_hold"), [])                  # másik mérésnél nem
        self.assertEqual(len(manager.candidates()), before)                          # az értékből nem lett jelölt
        manager.breakdown_feedback(cid(group), "subcategory", "1", "never")
        self.assertEqual(manager.good_dimensions("pickup_delay"), [])                # a "soha" mező kimarad

class RuleTest(unittest.TestCase):
    def setUp(self):
        self.manager = DiscoveryManager()

    def test_new_candidate_is_queued(self):
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY)
        queue = self.manager.queue()
        self.assertEqual([c.key for c in queue], ["assignment_group=G&weekday=Sun"])
        self.assertEqual(queue[0].state, CandidateState.QUEUED.value)

    def test_reject_pattern_creates_rule_that_hides_specializations(self):
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY)
        decision = self.manager.decide(cid(G_SUN), Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value)
        self.assertEqual(self.manager.candidate(cid(G_SUN)).state, CandidateState.REJECTED.value)
        self.assertIsNotNone(decision.rule_id)

        ingest(self.manager, [result(G_SUN_10, ["I4", "I5", "I6"]),
                              result(G_SUN, ["I7", "I8", "I9"], measure="sla_escalation")], DAY + timedelta(days=1))
        self.assertEqual(self.manager.candidate(cid(G_SUN_10)).state, CandidateState.FILTERED.value)
        # más mérésre nem vonatkozik
        self.assertEqual(self.manager.candidate(cid(G_SUN, "sla_escalation")).state, CandidateState.QUEUED.value)
        self.assertEqual([c.candidate_id for c in self.manager.covered_by(decision.rule_id)], [cid(G_SUN_10)])
        self.assertEqual(self.manager.state["rules"][decision.rule_id]["hits"], 1)

    def test_fact_rule_hides_time_sensitive_patterns_including_dates(self):
        g_date = (("assignment_group", "G"), ("date", "2026-08-09"))  # vasárnap
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"]), result(g_date, ["I4", "I5", "I6"]),
                              result(G_SUN, ["I7", "I8", "I9"], measure="sla_escalation")], DAY)
        decision = self.manager.decide(cid(G_SUN), Verdict.NOT_FINDING.value, Scope.RULE.value,
                                       fact=("nem_dolgozik", {"csoport": "G", "nap": "Sun"}))
        self.assertIsNotNone(decision.fact_id)
        self.assertEqual(self.manager.candidate(cid(g_date)).state, CandidateState.FILTERED.value)
        self.assertEqual(self.manager.candidate(cid(g_date)).filtered_by["fact_id"], decision.fact_id)
        # az SLA-eszkaláció nem időfüggő mérés, a rendelkezésre állás nem magyarázza
        self.assertEqual(self.manager.candidate(cid(G_SUN, "sla_escalation")).state, CandidateState.QUEUED.value)

    def test_boost_wins_and_extreme_breaks_through(self):
        strong = (("assignment_group", "G"), ("hour", "11"))
        # a nagy lift önmagában nem áttörő (kis mintán félrevezető), csak a statisztikai erősség
        weak_high_lift = (("assignment_group", "G"), ("hour", "12"))
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"]), result(G_SUN_10, ["I4", "I5", "I6"]),
                              result(strong, ["I7", "I8", "I9"], q=1e-9),
                              result(weak_high_lift, ["J1", "J2"], lift=40.0)], DAY)
        self.manager.add_rule([{"dim": "assignment_group", "values": ["G"]}])
        self.assertEqual(self.manager.candidate(cid(G_SUN)).state, CandidateState.FILTERED.value)
        self.assertEqual(self.manager.candidate(cid(strong)).state, CandidateState.QUEUED.value)
        self.assertIn("extreme_override", self.manager.candidate(cid(strong)).flags)
        self.assertEqual(self.manager.candidate(cid(weak_high_lift)).state, CandidateState.FILTERED.value)
        self.manager.add_rule([{"dim": "hour", "values": ["10"]}], action="boost")
        self.assertEqual(self.manager.candidate(cid(G_SUN_10)).state, CandidateState.QUEUED.value)

    def test_revoking_rule_restores_candidates(self):
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY)
        rule = self.manager.add_rule([{"dim": "weekday", "values": ["Sat", "Sun"]}])
        self.assertEqual(self.manager.candidate(cid(G_SUN)).state, CandidateState.FILTERED.value)
        revoked = self.manager.revoke_rule(rule.rule_id, "a csapat hétvégén is dolgozik")
        self.assertEqual((revoked.state, revoked.version, len(revoked.history)), ("revoked", 2, 1))
        self.assertEqual(self.manager.candidate(cid(G_SUN)).state, CandidateState.QUEUED.value)

    def test_rule_matches_alternative_description(self):
        merged = result((("business_service", "S"), ("weekday", "Sun")), ["I1", "I2", "I3"], alternatives=[G_SUN])
        ingest(self.manager, [merged], DAY)
        self.manager.add_fact("nem_dolgozik", {"csoport": "G", "nap": "Sun"})
        candidate = self.manager.candidate(make_candidate_id("pickup_delay", "business_service=S&weekday=Sun"))
        self.assertEqual(candidate.state, CandidateState.FILTERED.value)
        self.assertEqual(candidate.filtered_by["matched"], "assignment_group=G&weekday=Sun")

    def test_rule_render(self):
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY)
        self.manager.add_fact("nem_dolgozik", {"csoport": "G", "nap": "Sun"})
        rendered = [rule.render() for rule in self.manager.rules()]
        self.assertEqual(rendered, ["elvet(J) :- J.assignment_group = C, J.weekday = N, nem_dolgozik(C, N), időfüggő(J.mérés)."])
        self.assertEqual(self.manager.facts()[0].render(), "nem_dolgozik(G, Sun).")

class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.manager = DiscoveryManager()

    def test_rejected_once_returns_only_in_a_new_window(self):
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY)
        self.manager.decide(cid(G_SUN), Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value)
        ingest(self.manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY + timedelta(days=1))  # átfedő ablak
        self.assertEqual(self.manager.candidate(cid(G_SUN)).state, CandidateState.REJECTED_ONCE.value)
        report = ingest(self.manager, [result(G_SUN, ["I7", "I8", "I9"])], DAY + timedelta(days=8))
        self.assertEqual(report.returned, [cid(G_SUN)])
        self.assertIn("returned", self.manager.candidate(cid(G_SUN)).flags)

class SuggestionTest(unittest.TestCase):
    def test_similar_rejections_lower_priority_and_agreement_is_measured(self):
        manager = DiscoveryManager()
        g_sat = (("assignment_group", "G"), ("weekday", "Sat"))
        other = (("business_service", "X"), ("hour", "14"))
        ingest(manager, [result(g_sat, ["I1", "I2", "I3"])], DAY)
        # a tipp csak erős címkéből tanul (pl. "nem hiba" lezárás → szabály); a "most nem" nem címke
        manager.decide(cid(g_sat), Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value)

        ingest(manager, [result(G_SUN, ["I4", "I5", "I6"]), result(other, ["I7", "I8", "I9"])], DAY + timedelta(days=1))
        tip = manager.candidate(cid(G_SUN)).suggestion
        self.assertEqual(tip["verdict"], Verdict.NOT_FINDING.value)
        self.assertTrue(tip["reasons"][0].startswith("pickup_delay: assignment_group=G&weekday=Sat"))
        self.assertIsNone(manager.candidate(cid(other)).suggestion)
        self.assertLess(manager.candidate(cid(G_SUN)).priority, manager.candidate(cid(other)).priority)

        manager.decide(cid(G_SUN), Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value)
        stats = manager.learning_stats()
        self.assertEqual((stats["with_suggestion"], stats["agreement"]), (1, 1.0))

class NotNowTest(unittest.TestCase):
    def test_not_now_is_a_weak_negative_and_returning_candidate_climbs(self):
        manager = DiscoveryManager()
        g_sat = (("assignment_group", "G"), ("weekday", "Sat"))
        ingest(manager, [result(g_sat, ["I1", "I2", "I3"])], DAY)
        base = manager.candidate(cid(g_sat)).priority
        manager.decide(cid(g_sat), Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value)
        ingest(manager, [result(G_SUN, ["I4", "I5", "I6"])], DAY + timedelta(days=1))
        tip = manager.candidate(cid(G_SUN)).suggestion                     # a "most nem" gyenge ellenpélda
        self.assertEqual(tip["verdict"], Verdict.NOT_FINDING.value)
        self.assertLess(tip["confidence"], 0.3)
        ingest(manager, [result(g_sat, ["I1", "I2", "I3", "I7"])], DAY + timedelta(days=8))
        returned = manager.candidate(cid(g_sat))
        self.assertEqual((returned.state, returned.not_now), ("queued", 1))
        self.assertGreater(returned.priority, base)                         # visszatérve előrébb

class KnownDeviationTest(unittest.TestCase):
    """Egy tartósan kilógó csoport (szokásosan 4× az átlag) a saját szokásos eltéréséhez mérve."""

    def setUp(self):
        self.manager = DiscoveryManager()
        self.manager.baseline_prior = 0.0
        # Négy hét: a csoport 40-ből 8 rossz (20%), a populáció 1000-ből 50 (5%) -> szokásos lift 4
        for week in range(4):
            day = DAY + timedelta(days=7 * week)
            results = [result(G_SUN, [f"W{week}-{i}" for i in range(8)])]
            ingest(self.manager, results, day, {cid(G_SUN): counts(8, 40, 50, 1000)})

    def known(self):
        self.manager.decide(cid(G_SUN), Verdict.KNOWN_DEVIATION.value)
        return self.manager.candidate(cid(G_SUN))

    def test_counts_are_recorded_even_when_not_outstanding(self):
        ingest(self.manager, [], DAY + timedelta(days=28), {cid(G_SUN): counts(1, 40, 50, 1000)})
        candidate = self.manager.candidate(cid(G_SUN))
        self.assertEqual(len(candidate.counts), 5)
        self.assertEqual(len(candidate.occurrences), 4)  # kiugróként csak négyszer látta

    def test_baseline_is_pooled_and_frozen(self):
        candidate = self.known()
        self.assertEqual(candidate.state, CandidateState.BASELINED.value)
        self.assertAlmostEqual(candidate.baseline["lift"], 4.0)
        self.assertEqual((candidate.baseline["size"], candidate.baseline["windows"]), (160, 4))
        self.assertNotIn(cid(G_SUN), [c.candidate_id for c in self.manager.queue()])

    def test_overlapping_windows_count_once(self):
        # Napi futás 7 napos ablakkal: az átfedő ablakok nem számítanak külön
        ingest(self.manager, [], DAY + timedelta(days=22), {cid(G_SUN): counts(8, 40, 50, 1000)})
        self.assertEqual(self.known().baseline["windows"], 4)

    def test_too_little_data_waits_instead_of_refusing(self):
        self.manager.baseline_min_size = 200
        candidate = self.known()                                   # 160 eset van: a döntés rögzül, a norma vár
        self.assertEqual((candidate.state, candidate.baseline), (CandidateState.BASELINED.value, None))
        self.assertIn("baseline_pending", candidate.flags)
        self.assertNotIn(cid(G_SUN), [c.candidate_id for c in self.manager.queue()])
        self.assertEqual(self.manager.baseline_progress(candidate), {"size": 160, "windows": 4, "needed": 200})
        # a következő hét adatával összegyűlik: a norma magától befagy, és onnantól aszerint mér
        ingest(self.manager, [], DAY + timedelta(days=28), {cid(G_SUN): counts(8, 40, 50, 1000)})
        candidate = self.manager.candidate(cid(G_SUN))
        self.assertAlmostEqual(candidate.baseline["lift"], 4.0, places=1)
        self.assertNotIn("baseline_pending", candidate.flags)

    def test_systemwide_worsening_does_not_alert(self):
        self.known()
        # mindenki kétszer rosszabb: átlag 10%, a csoport 40% -> a szokásos 4× eltérésen belül
        report = ingest(self.manager, [], DAY + timedelta(days=28), {cid(G_SUN): counts(16, 40, 100, 1000)})
        self.assertEqual(report.exceeds_baseline, [])
        self.assertEqual(self.manager.candidate(cid(G_SUN)).state, CandidateState.BASELINED.value)

    def test_own_worsening_alerts(self):
        self.known()
        # az átlag marad 5%, a csoport 20% helyett 50%
        report = ingest(self.manager, [], DAY + timedelta(days=28), {cid(G_SUN): counts(20, 40, 62, 1000)})
        self.assertEqual(report.exceeds_baseline, [cid(G_SUN)])
        candidate = self.manager.candidate(cid(G_SUN))
        self.assertIn("exceeds_baseline", candidate.flags)
        self.assertIn(cid(G_SUN), [c.candidate_id for c in self.manager.queue()])
        self.assertGreater(candidate.stats["baseline_comparison"]["ratio"], 2)

class RefineTest(unittest.TestCase):
    def test_refined_pattern_stays_silent_and_becomes_context(self):
        manager = DiscoveryManager()
        sat = (("weekday", "Sat"),)
        ingest(manager, [result(sat, ["I1", "I2", "I3"])], DAY)
        manager.decide(cid(sat), Verdict.REFINE.value)
        self.assertEqual(manager.candidate(cid(sat)).state, CandidateState.REFINED.value)
        self.assertEqual(manager.contexts(), {"pickup_delay": [sat]})
        # újra kiugrik, új esetekkel: akkor sem jön vissza a sorba (nem figyelés, nem elvetés)
        ingest(manager, [result(sat, ["I1", "I2", "I3", "I4"])], DAY + timedelta(days=8))
        self.assertEqual(manager.candidate(cid(sat)).state, CandidateState.REFINED.value)
        self.assertEqual(manager.queue(), [])
        # nem tanuló döntés (a tipp nem tanul belőle), és visszavonható
        manager.unrefine(cid(sat))
        self.assertEqual(manager.candidate(cid(sat)).state, CandidateState.QUEUED.value)
        self.assertEqual(manager.contexts(), {})

class RevokeReopensTest(unittest.TestCase):
    def test_revoking_pattern_rule_brings_candidate_back(self):
        manager = DiscoveryManager()
        ingest(manager, [result(G_SUN, ["I1", "I2", "I3"])], DAY)
        rule_id = manager.decide(cid(G_SUN), Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value).rule_id
        self.assertEqual(manager.candidate(cid(G_SUN)).state, CandidateState.REJECTED.value)
        manager.revoke_rule(rule_id)
        candidate = manager.candidate(cid(G_SUN))
        self.assertEqual((candidate.state, candidate.flags), (CandidateState.QUEUED.value, ["rule_revoked"]))
        self.assertIn(cid(G_SUN), [c.candidate_id for c in manager._open_pool()])
        self.assertEqual(manager.reopen_rejected_by_revoked_rules(), [])   # ismételten hívható

class BreakdownFeedbackTest(unittest.TestCase):
    def test_feedback_is_learned_and_survives_reset(self):
        manager = DiscoveryManager()
        wed = (("weekday", "Wed"),)
        ingest(manager, [result(wed, ["I1", "I2", "I3"])], DAY)
        manager.decide(cid(wed), Verdict.REFINE.value)
        manager.breakdown_feedback(cid(wed), "business_service", "SX", "good")
        manager.breakdown_feedback(cid(wed), "subcategory", "1", "dead_end")
        manager.breakdown_feedback(cid(wed), "current_group", "G", "never")
        prefs = manager.breakdown_preferences()
        self.assertEqual(prefs["business_service"]["good"], 1)
        self.assertTrue(prefs["current_group"]["hidden"])
        self.assertEqual(manager.candidate(cid(wed)).stats["breakdown_hidden"], [["subcategory", "1"]])
        fresh = DiscoveryManager({}, knowledge=manager.knowledge)   # reset: a tanult szempontok megmaradnak
        self.assertEqual(fresh.breakdown_preferences(), prefs)
        with self.assertRaises(ValueError):
            manager.breakdown_feedback(cid(wed), "x", "y", "maybe")

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

class LevelFactTest(unittest.TestCase):
    def test_level_fact_classifies_without_creating_a_rule(self):
        manager = DiscoveryManager()
        manager.state["meta"]["now"] = "2026-08-10T00:00:00"
        manager.add_fact("szint", {"csoport": "L1", "szint": "L1"})
        self.assertEqual(dict(manager.knowledge_view().levels), {"L1": "L1"})
        self.assertEqual(manager.rules(), [])

    def test_not_working_fact_applies_to_stint_arrival_weekday(self):
        from tests.support.manager_fixtures import DAY, ingest, result
        manager = DiscoveryManager()
        a_sat = (("assignment_group", "A"), ("weekday", "Sat"))
        ingest(manager, [result(a_sat, ["I1", "I2", "I3"], measure="stint_pickup")], DAY)
        manager.add_fact("nem_dolgozik", {"csoport": "A", "nap": "Sat"})
        self.assertEqual(manager.candidate(make_candidate_id("stint_pickup", "assignment_group=A&weekday=Sat")).state,
                         CandidateState.FILTERED.value)


if __name__ == "__main__":
    unittest.main()
