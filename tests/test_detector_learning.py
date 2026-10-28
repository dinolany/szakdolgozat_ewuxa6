"""A gyári detektorok a tanuló körben: gazdagabb leírás, elnyomó szabály a valós idejű issue-nyitásban, almérés-egységek."""
from dataclasses import replace
from datetime import timedelta
import unittest

from learning.discovery_manager import DiscoveryManager
from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.core.suppression import suppressing_rule
from model.diagnostics.detectors.backlog_aging import BacklogAgingDiagnosis
from model.diagnostics.detectors.ping_pong import PingPongDiagnosis
from tests.test_core_diagnostics import BOUNCING, L1, T0, at, measured
from tests.test_discovery import make_incident
from tests.test_knowledge import DAY


class SignalItemsTest(unittest.TestCase):
    def test_ticket_detector_describes_route_groups_and_service(self):
        m = measured(PingPongDiagnosis(), [BOUNCING], 10, L1)["INC1"]
        items = PingPongDiagnosis().signal_items("INC1", m.details)
        self.assertIn(("incident", "INC1"), items)
        self.assertIn(("assignment_group", "B"), items)
        self.assertIn(("route", "L1 > A > B > C > D"), items)


class SuppressionTest(unittest.TestCase):
    RULE = {"rule_id": "RULE-1", "conditions": [{"dim": "assignment_group", "values": ["B"]}], "measures": ["PING_PONG"]}

    def test_rule_matches_any_of_the_groups_on_the_route(self):
        items = (("incident", "INC1"), ("assignment_group", "A"), ("assignment_group", "B"))
        self.assertEqual(suppressing_rule("PING_PONG", items, [self.RULE]), "RULE-1")
        self.assertIsNone(suppressing_rule("FLAPPING_INCIDENT", items, [self.RULE]))     # másik detektorra nem hat

    def test_suppressed_ticket_opens_no_issue_in_real_time(self):
        knowledge = replace(L1, suppressions=(self.RULE,))
        self.assertIn("INC1", measured(PingPongDiagnosis(), [BOUNCING], 10, L1))
        self.assertEqual(measured(PingPongDiagnosis(), [BOUNCING], 10, knowledge), {})

    def test_manager_rule_matches_multi_valued_signal(self):
        manager = DiscoveryManager()
        manager.ingest_signals([{"diagnostic": "PING_PONG", "subject": "INC1", "details": {}, "severity": "EXCEEDED_MODERATE",
                                 "description": "", "affected": ["INC1"],
                                 "items": [("incident", "INC1"), ("assignment_group", "A"), ("assignment_group", "B")]}], DAY)
        self.assertEqual(len(manager.queue()), 1)
        manager.add_rule([{"dim": "assignment_group", "values": ["A"]}], measures=["PING_PONG"])   # nem az utolsó érték
        self.assertEqual(manager.queue(), [])


class BacklogChildUnitsTest(unittest.TestCase):
    def test_aged_and_other_open_stints_per_group(self):
        old = make_incident("OLD", [(at(0), "state", None, "1"), (at(0), "assignment_group", "", "G")])
        new = make_incident("NEW", [(at(40), "state", None, "1"), (at(40), "assignment_group", "", "G")])
        ctx = MeasurementContext(Trigger.MANUAL, T0 + timedelta(hours=48), [old, new], [], knowledge=L1)
        aged, other = BacklogAgingDiagnosis().child_units(ctx)["G"]
        self.assertEqual(([i for i, _ in aged], [i for i, _ in other]), (["OLD"], ["NEW"]))


class BacklogWorkingHoursTest(unittest.TestCase):
    def test_age_counts_working_hours_when_schedule_is_known(self):
        from datetime import datetime
        from model.knowledge.group_schedules import GroupSchedules
        from model.knowledge.knowledge_view import KnowledgeView
        from model.working_time.schedule import Schedule
        friday = datetime(2026, 7, 31, 16)
        incidents = [make_incident(f"I{i}", [(friday.isoformat(), "state", None, "1"), (friday.isoformat(), "assignment_group", "", "G")])
                     for i in range(2)]
        ctx = lambda knowledge: MeasurementContext(Trigger.MANUAL, datetime(2026, 8, 3, 10), incidents, [],
                                                   thresholds={"moderate_age_hours": 24, "minimum_count": 2}, knowledge=knowledge)
        wall = list(BacklogAgingDiagnosis()._observe(ctx(KnowledgeView()), None))[0].details
        office = KnowledgeView(schedules=GroupSchedules({"*": Schedule(frozenset(range(5)), 8, 17)}))
        work = list(BacklogAgingDiagnosis()._observe(ctx(office), None))[0].details
        self.assertEqual((wall["aged"], round(wall["oldest_hours"]), wall["time_basis"]), (2, 66, "falióra"))
        self.assertEqual((work["aged"], work["time_basis"]), (0, "munkaóra"))   # péntek 16 -> hétfő 10: 3 munkaóra


class GoodDimensionTest(unittest.TestCase):
    def test_good_dimension_is_remembered_per_measure_not_per_value(self):
        from tests.test_knowledge import cid, ingest, result
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


class LearnedThresholdTest(unittest.TestCase):
    def test_suggests_where_exceeding_is_the_norm(self):
        from learning.feedback.learned_thresholds import suggestions
        base = {"moderate_moves": 3, "extreme_moves": 5}
        busy = [((("business_service", "S"),), v) for v in (3, 4, 4, 5, 3, 6, 4, 2, 3, 7)]      # a jegyek többsége >= 3
        calm = [((("business_service", "Q"),), v) for v in (0, 1, 0, 3, 1, 0, 2, 0, 1, 0)]
        found = suggestions("PING_PONG", "Ping-pong", base, busy + calm)
        self.assertEqual([s["scope"] for s in found], [[["business_service", "S"]]])
        self.assertEqual(found[0]["params"], {"moderate_moves": 6, "extreme_moves": 8})   # 90. percentilis, az extrém +2
        self.assertEqual(suggestions("PING_PONG", "Ping-pong", base, busy, existing_scopes=[(("business_service", "S"),)]), [])
        self.assertEqual(suggestions("PING_PONG", "Ping-pong", base, busy, dismissed={"PING_PONG|business_service=S": "6"}), [])
        # friss túllépés (minden túllépő jegy egy héten belül): lehet, hogy hiba, ezért nincs javaslat
        recent = [(items, v, DAY + timedelta(days=i % 5)) for i, (items, v) in enumerate(busy)]
        lasting = [(items, v, DAY + timedelta(days=3 * i)) for i, (items, v) in enumerate(busy)]
        self.assertEqual(suggestions("PING_PONG", "Ping-pong", base, recent), [])
        self.assertEqual(len(suggestions("PING_PONG", "Ping-pong", base, lasting)), 1)


class KnownFaultRealTimeTest(unittest.TestCase):
    def test_confirmed_issue_reopens_in_the_batch_when_fault_returns_after_quiet(self):
        from engine.issue_manager import IssueManager
        from model.issue_state import IssueState
        diagnostic, manager, db = PingPongDiagnosis(), IssueManager(), {"issues": {}, "issue_history": []}
        run = lambda limit: manager.apply_run(db, diagnostic.run(MeasurementContext(
            Trigger.EVENT_BATCH, T0 + timedelta(hours=10), [BOUNCING], list(db["issues"].values()),
            thresholds={"moderate_moves": limit, "extreme_moves": 9}, knowledge=L1)), "köteg")
        run(3)
        issue = db["issues"]["ISSUE-PING_PONG-INC1"]
        issue.state = IssueState.RESOLVED
        issue.details["lesson"] = {"outcome": "confirmed"}
        run(3)                                                     # még túllép: nem csendesedett el, nem nyílik újra
        self.assertEqual(db["issues"]["ISSUE-PING_PONG-INC1"].state, IssueState.RESOLVED)
        run(5)                                                     # küszöb alatt: elcsendesedett (a jelző megmarad)
        self.assertIn("known_fault_quiet_since", db["issues"]["ISSUE-PING_PONG-INC1"].details)
        changes = run(3)                                           # újra túllép: ugyanebben a kötegben újranyílik
        reopened = db["issues"]["ISSUE-PING_PONG-INC1"]
        self.assertEqual(reopened.state, IssueState.ACTIVE)
        self.assertIn("known_fault_returned_at", reopened.details)
        self.assertEqual(reopened.details["lesson"]["outcome"], "confirmed")
        self.assertTrue(any(c.get("reason") == "ismert hiba visszatért" for c in changes))


if __name__ == "__main__":
    unittest.main()
