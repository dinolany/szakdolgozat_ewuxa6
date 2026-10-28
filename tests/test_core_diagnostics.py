import unittest
from datetime import datetime, timedelta

from issue_manager import IssueManager
from models.diagnostics.backlog_aging import BacklogAgingDiagnosis
from models.diagnostics.flapping import FlappingDiagnosis
from models.diagnostics.issue_trend import IssueTrendDiagnosis
from models.diagnostics.registry import DiagnosticRegistry
from models.diagnostics.similar_description import SimilarDescriptionDiagnosis
from models.diagnostics.ticket_volume import TicketVolumeDiagnosis
from models.diagnostics.core import MeasurementContext, Trigger
from models.diagnostics.ping_pong import PingPongDiagnosis
from models.diagnostics.unassigned import UnassignedDiagnosis
from models.knowledge_base import KnowledgeView
from tests.test_discovery import make_incident

T0 = datetime(2026, 8, 3, 8)
L1 = KnowledgeView(levels={"L1": "L1"})


def at(hours):
    return (T0 + timedelta(hours=hours)).isoformat()


def measured(diagnostic, incidents, hours, knowledge=KnowledgeView()):
    run = diagnostic.run(MeasurementContext(Trigger.MANUAL, T0 + timedelta(hours=hours), incidents, [], knowledge=knowledge))
    return {m.subject: m for m in run.measurements if m.exceeded}


BOUNCING = make_incident("INC1", [
    (at(0), "assignment_group", "", "L1"),
    (at(1), "assignment_group", "L1", "A"),
    (at(2), "assignment_group", "A", "B"),
    (at(3), "assignment_group", "B", "L1"),   # vissza az L1-be
    (at(4), "assignment_group", "L1", "C"),
    (at(9), "assignment_group", "C", "D"),
])


class PingPongTest(unittest.TestCase):
    def test_l1_dispatch_is_not_counted_and_returns_are(self):
        unknown = measured(PingPongDiagnosis(), [BOUNCING], 10)["INC1"]
        known = measured(PingPongDiagnosis(), [BOUNCING], 10, L1)["INC1"]
        self.assertEqual(unknown.details["moves"], 5)
        self.assertEqual((known.details["moves"], known.details["returned_to_l1"]), (3, 1))
        self.assertEqual(known.issue_id, "ISSUE-PING_PONG-INC1")       # a régi azonosítóformátum
        self.assertIn("vissza az L1-be", known.description)

    def test_as_of_ignores_later_moves(self):
        self.assertEqual(measured(PingPongDiagnosis(), [BOUNCING], 5, L1), {})  # 5 órakor még csak 2 váltás


class UnassignedTest(unittest.TestCase):
    def test_open_ticket_without_group_as_of_measurement(self):
        incident = make_incident("INC1", [
            (at(0), "state", None, "1"),
            (at(0), "assignment_group", "", "A"),
            (at(2), "assignment_group", "A", ""),   # a csoportot törölték
            (at(10), "assignment_group", "", "B"),
        ])
        self.assertEqual(measured(UnassignedDiagnosis(), [incident], 1), {})
        found = measured(UnassignedDiagnosis(), [incident], 7)["INC1"]
        self.assertEqual((found.details["hours"], str(found.severity)), (5.0, "EXCEEDED_EXTREME"))
        self.assertEqual(found.issue_id, "ISSUE-UNASSIGNED_TICKET-INC1:UNASSIGNED_TICKET")
        self.assertEqual(measured(UnassignedDiagnosis(), [incident], 11), {})  # azóta kapott csoportot


class BacklogAgingTest(unittest.TestCase):
    def ticket(self, incident_id, arrival_hours, hold_hours=0):
        events = [(at(0), "state", None, "1"), (at(0), "assignment_group", "", "L1"),
                  (at(arrival_hours), "assignment_group", "L1", "A")]
        if hold_hours:
            events += [(at(arrival_hours + 1), "state", "1", "3"), (at(arrival_hours + 1 + hold_hours), "state", "3", "2")]
        return make_incident(incident_id, events)

    def test_waiting_is_measured_at_the_current_group_without_hold(self):
        incidents = [
            self.ticket("INC1", 1),                    # 99 órája A-nál
            self.ticket("INC2", 70),                   # 30 órája A-nál
            self.ticket("INC3", 1, hold_hours=90),     # 99 óra A-nál, de ebből 90 hold: 9 óra aktív várakozás
            self.ticket("INC4", 99),                   # 1 órája A-nál (előtte 99 óra az L1-ben)
        ]
        found = measured(BacklogAgingDiagnosis(), incidents, 100)["A"]
        self.assertEqual((found.details["open"], found.details["aged"]), (4, 2))
        self.assertEqual(set(found.affected_objects), {"INC1", "INC2"})
        self.assertEqual(str(found.severity), "EXCEEDED_EXTREME")      # 99 óra >= 72
        self.assertEqual(found.issue_id, "ISSUE-BACKLOG_AGING-A")


class FlappingTest(unittest.TestCase):
    def test_reopen_counted_once_despite_duplicate_state_fields(self):
        events = [(at(0), "state", None, "1")]
        for hour in (2, 4):
            events += [(at(hour), "state", "1", "6"), (at(hour), "incident_state", "1", "6"),
                       (at(hour + 1), "state", "6", "2"), (at(hour + 1), "incident_state", "6", "2")]
        found = measured(FlappingDiagnosis(), [make_incident("INC1", events)], 10)["INC1"]
        self.assertEqual(found.details["transition_count"], 2)
        self.assertEqual(found.issue_id, "ISSUE-FLAPPING_INCIDENT-INC1:FLAPPING_INCIDENT")


class TicketVolumeTest(unittest.TestCase):
    def test_bucket_compared_with_daily_average_of_same_hour_and_day_type(self):
        incidents = []
        for day in range(5):                         # hétfő-péntek 10-11 között naponta 2 ticket
            for minute in (5, 35):
                opened = datetime(2026, 8, 3 + day, 10, minute)
                incidents.append(make_incident(f"H{day}{minute}", [(opened.isoformat(), "state", None, "1")]))
        for minute in range(0, 60, 6):               # a következő hétfőn 10 ticket ugyanabban az órában
            opened = datetime(2026, 8, 10, 10, minute)
            incidents.append(make_incident(f"N{minute}", [(opened.isoformat(), "state", None, "1")]))
        run = TicketVolumeDiagnosis().run(MeasurementContext(Trigger.MANUAL, datetime(2026, 8, 10, 11, 30), incidents, []))
        [found] = [m for m in run.measurements if m.exceeded]
        self.assertEqual((found.details["openings"], found.details["expected"], found.details["history_days"]), (10, 2.0, 5))
        self.assertEqual(str(found.severity), "EXCEEDED_EXTREME")   # +400%


class SimilarDescriptionTest(unittest.TestCase):
    def test_first_description_within_window(self):
        def ticket(incident_id, hours, text, later=None):
            events = [(at(hours), "short_description", "", text)]
            if later:
                events.append((at(hours + 1), "short_description", text, later))
            return make_incident(incident_id, events)
        incidents = [ticket("I1", 0, "VPN nem megy"), ticket("I2", 1, "vpn nem megy  ", later="Megoldva"),
                     ticket("I3", 2, "VPN NEM MEGY"), ticket("I4", -48, "VPN nem megy")]   # I4 az ablakon kívül
        found = measured(SimilarDescriptionDiagnosis(), incidents, 5)
        [issue] = found.values()
        self.assertEqual(issue.affected_objects, ("I1", "I2", "I3"))


class IssueTrendTest(unittest.TestCase):
    def test_counts_active_issues_per_category(self):
        incidents = [make_incident(f"INC{i}", [(at(0), "assignment_group", "", "L1"), (at(1), "assignment_group", "L1", "A"),
                                               (at(2), "assignment_group", "A", "B"), (at(3), "assignment_group", "B", "C"),
                                               (at(4), "assignment_group", "C", "D")])
                     for i in range(6)]
        registry = DiagnosticRegistry([PingPongDiagnosis(), IssueTrendDiagnosis()])
        db = {"issues": {}, "issue_history": []}
        registry.execute(db, IssueManager(), Trigger.MANUAL, T0 + timedelta(hours=5), incidents, {}, knowledge=L1)
        trend = [i for i in db["issues"].values() if i.diagnostic_key == "ISSUE_TREND"]
        self.assertEqual([(i.subject, i.details["count"]) for i in trend], [("PING_PONG", 6)])


if __name__ == "__main__":
    unittest.main()
