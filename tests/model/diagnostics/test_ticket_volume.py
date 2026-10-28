"""Tesztek: model/diagnostics/ticket_volume.py."""
from datetime import datetime
import unittest

from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.detectors.ticket_volume import TicketVolumeDiagnosis
from tests.support.profile_fixtures import make_incident


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


if __name__ == "__main__":
    unittest.main()
