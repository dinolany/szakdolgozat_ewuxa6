"""Tesztek: model/diagnostics/backlog_aging.py."""
from datetime import datetime, timedelta
import unittest

from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.detectors.backlog_aging import BacklogAgingDiagnosis
from model.knowledge.knowledge_view import KnowledgeView
from tests.support.diagnostic_fixtures import L1, T0, at, measured
from tests.support.profile_fixtures import make_incident


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


if __name__ == "__main__":
    unittest.main()
