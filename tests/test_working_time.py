import unittest
from collections import Counter
from datetime import datetime

from models.knowledge_base import KnowledgeBase
from models.working_time import Schedule, fact_args, infer_schedule, schedule_from_fact

WEEKDAYS_8_16 = Schedule(frozenset(range(5)), 8, 16)


class ScheduleTest(unittest.TestCase):
    def test_friday_afternoon_to_monday_morning_is_one_working_hour(self):
        friday, monday = datetime(2026, 8, 7, 15, 30), datetime(2026, 8, 10, 8, 30)
        self.assertAlmostEqual(WEEKDAYS_8_16.working_hours(friday, monday), 1.0)

    def test_continuous_and_night_shift(self):
        always = Schedule(frozenset(range(7)), 0, 24)
        self.assertTrue(always.continuous)
        self.assertAlmostEqual(always.working_hours(datetime(2026, 8, 8, 10), datetime(2026, 8, 9, 10)), 24)
        night = Schedule(frozenset(range(7)), 22, 6)   # 22–06
        self.assertAlmostEqual(night.working_hours(datetime(2026, 8, 8, 20), datetime(2026, 8, 9, 8)), 8)

    def test_fact_roundtrip_and_knowledge_view(self):
        args = fact_args("G", WEEKDAYS_8_16)
        self.assertEqual(args, {"csoport": "G", "napok": "Mon,Tue,Wed,Thu,Fri", "tol": "8", "ig": "16"})
        self.assertEqual(schedule_from_fact(args).label(), "hétfő–péntek 8–16")
        kb = KnowledgeBase()
        kb.add_fact("munkaido", args, note="kézi")
        self.assertEqual(kb.view().schedules["G"].days, frozenset(range(5)))

    def test_infer_weekday_daytime_group(self):
        counts = Counter()
        for day in range(5):
            for hour in range(8, 17):
                counts[(day, hour)] += 10
        counts[(5, 11)] += 1   # egy elszórt szombati művelet
        suggestion = infer_schedule(counts, coverage=0.9)
        self.assertEqual(suggestion.schedule.days, frozenset(range(5)))
        self.assertEqual((suggestion.schedule.start, suggestion.schedule.end), (8, 17))
        self.assertGreater(suggestion.coverage, 0.95)

    def test_infer_round_the_clock_group(self):
        counts = Counter({(d, h): 5 for d in range(7) for h in range(24)})
        self.assertTrue(infer_schedule(counts).schedule.continuous)


class StintPickupInWorkingHoursTest(unittest.TestCase):
    def test_pickup_measured_in_working_hours(self):
        from models.discovery.profile import stint_profiles
        from models.timeline import incident_timeline
        from tests.test_discovery import make_incident
        events = [("2026-08-07T15:00:00", "state", None, "1"), ("2026-08-07T15:00:00", "assignment_group", "", "L1"),
                  ("2026-08-07T15:30:00", "assignment_group", "L1", "G"), ("2026-08-10T08:30:00", "assigned_to", "", "agent")]
        timeline = incident_timeline(make_incident("I1", events), datetime(2026, 8, 11))
        wall = {p.dimensions["assignment_group"]: p for p in stint_profiles(timeline, {"L1": "L1"})}
        work = {p.dimensions["assignment_group"]: p for p in stint_profiles(timeline, {"L1": "L1"}, {"G": WEEKDAYS_8_16})}
        self.assertAlmostEqual(wall["G"].measures["stint_pickup"], 65.0)
        self.assertAlmostEqual(work["G"].measures["stint_pickup"], 1.0)
        self.assertEqual((wall["G"].dimensions["time_basis"], work["G"].dimensions["time_basis"]), ("falióra", "munkaóra"))


class CompanyScheduleTest(unittest.TestCase):
    def test_company_schedule_applies_where_group_has_none(self):
        from models.knowledge_base import KnowledgeBase
        kb = KnowledgeBase()
        kb.add_fact("munkaido", {"csoport": "*", "napok": "Mon,Tue,Wed,Thu,Fri", "tol": "8", "ig": "17"})
        kb.add_fact("munkaido", {"csoport": "NIGHT", "napok": "*", "tol": "20", "ig": "6"})
        schedules = kb.view().schedules
        self.assertEqual((schedules.get("G").start, schedules.get("G").end), (8, 17))       # cégszintű
        self.assertEqual(schedules.get("NIGHT").start, 20)                                    # a saját nyer
        self.assertNotIn("G", schedules)                                                       # a javaslatok a sajátot nézik


if __name__ == "__main__":
    unittest.main()


class TwoShiftTest(unittest.TestCase):
    def test_two_shifts_are_not_continuous(self):
        counts = Counter({(d, h): 5 for d in range(7) for h in range(6, 22)})
        suggestion = infer_schedule(counts)
        self.assertFalse(suggestion.schedule.continuous)
        self.assertEqual(suggestion.schedule.days, frozenset(range(7)))
