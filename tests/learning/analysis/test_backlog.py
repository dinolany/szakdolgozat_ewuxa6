"""Tesztek: learning/analysis/backlog.py."""
from datetime import datetime
import unittest

from learning.analysis.backlog import calendar, time_marks


class BacklogCalendarTest(unittest.TestCase):
    def test_waiting_open_and_arrivals_per_hour(self):
        stints = [
            {"incident_id": "I1", "index": 1, "start": datetime(2026, 8, 7, 15, 10), "pickup": datetime(2026, 8, 10, 8, 30),
             "end": datetime(2026, 8, 10, 12)},   # péntek délután érkezik, hétfőn veszik fel
            {"incident_id": "I2", "index": 1, "start": datetime(2026, 8, 8, 9, 0), "pickup": None, "end": datetime(2026, 8, 8, 9, 50)},
        ]
        cells = {(c["day"], c["hour"]): c for c in calendar(stints, datetime(2026, 8, 7), 4)}
        self.assertEqual(cells[("2026-08-07", 15)]["arrivals"], 1)
        self.assertEqual(cells[("2026-08-08", 3)]["waiting"], 1)      # szombat hajnal: várakozik
        self.assertEqual(cells[("2026-08-08", 9)]["waiting"], 2)      # + a felvetlenül továbbadott második
        self.assertEqual(cells[("2026-08-10", 9)], {"day": "2026-08-10", "hour": 9, "waiting": 0, "open": 1, "arrivals": 0})

    def test_time_marks(self):
        marks = time_marks([("weekday", "Sat"), ("hour", "15"), ("date", "2026-08-08"), ("assignment_group", "G")])
        self.assertEqual(marks, {"dates": ["2026-08-08"], "weekdays": [5], "hours": [15]})


if __name__ == "__main__":
    unittest.main()
