import unittest
from datetime import datetime, timedelta

from models.discovery.children import (ChildDef, UnitContext, compile_expression, evaluate, rows_to_expression,
                                       run_child, trigger_matches, trigger_options)
from models.discovery.knowledge import CandidateState, Verdict
from models.discovery.manager import DiscoveryManager
from models.timeline import incident_timeline
from tests.test_discovery import make_incident
from tests.test_knowledge import DAY, cid, ingest, result

START = datetime(2026, 8, 3, 9)
AS_OF = datetime(2026, 8, 20)


def quick_close(incident_id, minutes, close_notes=True, work_notes=False):
    """L1 továbbítja a G csoportnak, ott `minutes` perc után lezárják."""
    t = lambda m: (START + timedelta(minutes=m)).isoformat()
    events = [
        (t(0), "state", None, "1"),
        (t(0), "assignment_group", "", "L1"),
        (t(5), "assignment_group", "L1", "G"),
        (t(6), "assigned_to", "", "resolver"),
    ]
    if work_notes:
        events.append((t(7), "work_notes", "", "megnéztem"))
    if close_notes:
        events.append((t(5 + minutes), "close_notes", "", "kész"))
    events.append((t(5 + minutes), "state", "1", "6"))
    return incident_timeline(make_incident(incident_id, events), AS_OF)


IMMEDIATE = 'szakasz_perc < 15 and lezarta and kitoltve("close_notes")'


class LanguageTest(unittest.TestCase):
    def test_rejects_unknown_and_unsafe(self):
        for bad in ('__import__("os")', "open('x')", "x.y", "lambda: 1", "unknown_var > 1", "ertek(1)[0]"):
            with self.assertRaises(ValueError, msg=bad):
                compile_expression(bad)

    def test_rows_compile_to_same_meaning(self):
        expression = rows_to_expression([{"var": "szakasz_perc", "op": "<", "value": 15}, {"var": "lezarta"},
                                         {"fn": "kitoltve", "arg": "close_notes"}])
        timeline = quick_close("I1", 10)
        unit = UnitContext(timeline, timeline.stints[1], {})
        self.assertTrue(evaluate(compile_expression(expression), unit))
        self.assertEqual(evaluate(compile_expression(expression), unit), evaluate(compile_expression(IMMEDIATE), unit))

    def test_stint_vocabulary(self):
        timeline = quick_close("I1", 10)
        g = UnitContext(timeline, timeline.stints[1], {"L1": "L1"})
        self.assertEqual(g.timeline.stints[1].group, "G")
        check = lambda text: evaluate(compile_expression(text), g)
        self.assertTrue(check(IMMEDIATE))
        self.assertTrue(check('felvetel_perc == 1 and csoport == "G" and not tovabbitotta'))
        self.assertTrue(check('ertek("state") in ["6", "7"] and nyito_csoport == "L1" and zaro_csoport == "G"'))
        self.assertFalse(check('kitoltve("work_notes")'))
        self.assertTrue(check('valtozasok("close_notes") == 1 and csoportok_szama == 2 and not nyit_zar_egyedul'))
        slow = quick_close("I2", 120)
        self.assertFalse(evaluate(compile_expression(IMMEDIATE), UnitContext(slow, slow.stints[1], {})))
        # az L1 szakasza továbbítással ért véget, nem zárta le
        self.assertTrue(evaluate(compile_expression("tovabbitotta and kovetkezo_csoport == 'G'"),
                                 UnitContext(timeline, timeline.stints[0], {})))

    def test_missing_value_is_not_comparable(self):
        timeline = quick_close("I1", 10)
        unit = UnitContext(timeline, timeline.stints[0], {})   # az L1 szakaszában nem lett felelős (üres érték)
        self.assertFalse(evaluate(compile_expression("felvetel_perc > 1000"), unit))


class RunTest(unittest.TestCase):
    def test_affected_vs_reference(self):
        timelines = {f"A{i}": quick_close(f"A{i}", 5) for i in range(8)}
        timelines.update({f"A{i}": quick_close(f"A{i}", 200) for i in range(8, 10)})
        timelines.update({f"R{i}": quick_close(f"R{i}", 300, work_notes=True) for i in range(10)})
        child = ChildDef("CHD-1", "Azonnali zárás", IMMEDIATE, "explain", 0.6)
        out = run_child(child, [(f"A{i}", 1) for i in range(10)], [(f"R{i}", 1) for i in range(10)],
                        timelines.get, {}, AS_OF.isoformat())
        self.assertEqual((out["affected_true"], out["affected_n"], out["reference_true"], out["reference_n"]), (8, 10, 0, 10))
        self.assertTrue(out["triggered"])
        self.assertIn("80%", out["text"])
        self.assertIn("magyarázat", out["text"])


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


if __name__ == "__main__":
    unittest.main()


class DimensionAndReviewTest(unittest.TestCase):
    def test_grouping_child_as_search_dimension_uses_its_value(self):
        from models.discovery.profile import build_profiles
        from models.knowledge_base import KnowledgeBase
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
        from models.discovery.profile import build_profiles
        from models.knowledge_base import KnowledgeBase
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
        from models.discovery import MiningParams, mine
        from tests.test_discovery import PICKUP, make_profile
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
        from models.discovery.children import ChildReviewParams, review_children
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


class FriendlySyntaxTest(unittest.TestCase):
    def test_bare_field_names_in_functions(self):
        timeline = quick_close("I1", 10)
        unit = UnitContext(timeline, None, {})   # ticket egység
        self.assertTrue(evaluate(compile_expression("jegy_lezarva and valtozasok(assignment_group) == 2"), unit))
        self.assertTrue(evaluate(compile_expression('kitoltve(close_notes) and ertek(state) == "6"'), unit))
        # a szókincs változói továbbra is változók maradnak
        self.assertTrue(evaluate(compile_expression("szam(csoportok_szama) == 2"), unit))


class ReviewConfigTest(unittest.TestCase):
    def test_review_params_ignore_child_keys(self):
        import tempfile
        from pathlib import Path
        from models.discovery.config import DiscoveryConfig
        with tempfile.TemporaryDirectory() as tmp:
            config = DiscoveryConfig(str(Path(tmp) / "discovery.json"))
            self.assertEqual(config.review_params().min_contradictions, 3)
            self.assertEqual(config.child_review_params().min_runs, 10)


class AttachTest(unittest.TestCase):
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


def stint_case(steps):
    """L1 -> G; G-nél a 10. percben névre veszik, utána `steps` (perc, mező, régi, új)."""
    t = lambda m: (START + timedelta(minutes=m)).isoformat()
    events = [(t(0), "state", None, "1"), (t(0), "assignment_group", "", "L1"), (t(5), "assignment_group", "L1", "G"),
              (t(10), "assigned_to", "", "resolver"), (t(11), "email", "x", "")]   # az email rendszerzaj
    events += [(t(m), f, o, n) for m, f, o, n in steps]
    timeline = incident_timeline(make_incident("I1", events), AS_OF)
    return UnitContext(timeline, timeline.stints[1], {})


class InteractionVocabularyTest(unittest.TestCase):
    def check(self, unit, text):
        return evaluate(compile_expression(text), unit)

    def test_first_step_after_pickup(self):
        passed = stint_case([(12, "assignment_group", "G", "H"), (12, "u_routing_instructions", "", "R")])
        self.assertTrue(self.check(passed, 'elso_lepes_felvetel_utan == "tovabbitas" and interakciok_felvetel_utan == 0'))
        worked = stint_case([(20, "work_notes", "", "megnéztem"), (40, "close_notes", "", "kész"), (40, "state", "1", "6")])
        self.assertTrue(self.check(worked, 'elso_lepes_felvetel_utan == "interakcio" and interakciok == 1 and lezarta'))
        closed = stint_case([(13, "close_notes", "", "kész"), (13, "work_notes", "", "zárom"), (13, "state", "1", "7")])
        self.assertTrue(self.check(closed, 'elso_lepes_felvetel_utan == "lezaras" and interakciok == 0 and szakasz_perc <= 30'))
        held = stint_case([(15, "state", "1", "3"), (16, "u_pending_reason", "", "100")])
        self.assertTrue(self.check(held, 'elso_lepes_felvetel_utan == "allapot" and interakciok_felvetel_utan == 1'))
        idle = stint_case([])
        self.assertTrue(self.check(idle, 'elso_lepes_felvetel_utan == "nincs" and felvetel_utan_perc > 0'))


class InProgressIsNotAStepTest(unittest.TestCase):
    def test_in_progress_is_skipped(self):
        unit = stint_case([(10, "state", "1", "2"), (12, "assignment_group", "G", "H")])
        self.assertTrue(evaluate(compile_expression('elso_lepes_felvetel_utan == "tovabbitas"'), unit))


def signal(affected, group="G"):
    return {"diagnostic": "FEATURE_USAGE", "subject": f"work_notes|{group}",
            "items": (("feature", "work_notes"), ("assignment_group", group)),
            "details": {"feature": "work_notes", "group": group}, "severity": "EXCEEDED_EXTREME",
            "description": "sosem ír munkajegyzetet", "affected": affected}


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


class TimeVocabularyTest(unittest.TestCase):
    """START = 2026-08-03 hétfő 9:00; L1 -> G az 5. percben, névre vétel a 6. percben."""

    def test_previous_group_and_moments(self):
        timeline = quick_close("I1", 10)
        g = UnitContext(timeline, timeline.stints[1], {"L1": "L1"})
        check = lambda text: evaluate(compile_expression(text), g)
        self.assertTrue(check('elozo_csoport == "L1" and elozo_szint == "L1" and nyito_szint == "L1"'))
        self.assertTrue(check('nyitas_nap == "hétfő" and nyitas_ora == 9 and not nyitas_hetvege'))
        self.assertTrue(check('kapta_munkaidoben and felvette_munkaidoben and vege_ora == 9'))
        self.assertTrue(check("felvetel_munkaperc == 1 and not munkarend_ismert"))
        first = UnitContext(timeline, timeline.stints[0], {"L1": "L1"})
        self.assertEqual(evaluate(compile_expression('elozo_csoport == ""'), first), True)

    def test_group_schedule_decides_working_time(self):
        from models.working_time import Schedule
        timeline = quick_close("I1", 10)
        night = {"G": Schedule(frozenset(range(7)), 20, 6)}   # éjszakás csoport: hétfő 9-kor nincs munkaidő
        g = UnitContext(timeline, timeline.stints[1], {}, night)
        check = lambda text: evaluate(compile_expression(text), g)
        self.assertTrue(check("munkarend_ismert and kapta_munkaidoben == False and felvetel_munkaperc == 0"))


class GroupMethodTest(unittest.TestCase):
    def test_grouping_shows_distribution_and_triggers_on_concentration(self):
        timelines = {f"A{i}": quick_close(f"A{i}", 10) for i in range(4)}
        timelines.update({f"R{i}": quick_close(f"R{i}", 10) for i in range(4)})
        child = ChildDef("CHD-X", "Ráadó csoport szerint", "elozo_csoport", threshold=0.6, method="group")
        out = run_child(child, [(k, 1) for k in timelines if k.startswith("A")], [(k, 0) for k in timelines if k.startswith("R")],
                        timelines.get, {}, "2026-08-20")
        self.assertTrue(out["triggered"])                                    # minden érintettet az L1 adott rá
        self.assertEqual(out["groups"][0]["value"], "L1")
        self.assertEqual((out["groups"][0]["share"], out["groups"][0]["reference_share"]), (1.0, 0.0))
        self.assertIn("L1 100%", out["text"])
