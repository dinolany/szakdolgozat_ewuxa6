from datetime import datetime, timedelta
import time
import unittest

from simulation.simulation_scheduler import SimulationScheduler


START = datetime(2026, 8, 3, 6)
END = datetime(2026, 8, 6, 6)          # három nap


class FakeQueue:
    """Óránként egy esemény a két határ között."""

    def __init__(self):
        self.events = [START + timedelta(hours=h) for h in range(int((END - START).total_seconds() // 3600) + 1)]

    def load(self, timeline):
        return self.events

    def get_bounds(self):
        return {"start": START, "end": END}

    def pop_due_events(self, current, processed):
        index = processed
        while index < len(self.events) and self.events[index] <= current:
            index += 1
        return self.events[processed:index], index

    def clear(self):
        pass


class FakeTransformer:
    def get_timeline(self):
        return []

    def clear_cache(self):
        pass


class FakeProcessor:
    def __init__(self):
        self.seen = []

    def process_events(self, events):
        self.seen.extend(events)


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class StepDayTest(unittest.TestCase):
    def setUp(self):
        self.processor = FakeProcessor()
        self.day_ends = []
        self.scheduler = SimulationScheduler(FakeTransformer(), FakeQueue(), self.processor, repository=None,
                                             on_day_end=self.day_ends.append)
        self.scheduler._duration_seconds = 3   # gyors próba: 3 nap 3 másodperc alatt

    def tearDown(self):
        self.scheduler.stop()

    def test_stops_exactly_at_day_end_and_continues(self):
        self.scheduler._start(3)
        self.scheduler.pause()
        status = self.scheduler.step_day(600)
        self.assertEqual(status["pause_at"], "04.08.2026 00:00:00")
        self.assertTrue(wait_until(lambda: self.scheduler.get_status()["pause_reason"] == "day_end"))
        status = self.scheduler.get_status()
        self.assertEqual((status["state"], status["current_time"]), ("paused", "04.08.2026 00:00:00"))
        self.assertEqual(self.day_ends, [datetime(2026, 8, 4)])
        self.assertEqual(max(self.processor.seen), datetime(2026, 8, 4))   # éjfélig bezárólag (a feltárás is így lát)
        self.assertEqual(len(self.processor.seen), 19)          # 06:00..00:00
        # szünetben nem ugrik előre a virtuális idő
        time.sleep(0.3)
        self.assertEqual(self.scheduler.get_status()["current_time"], "04.08.2026 00:00:00")
        # következő lépés: a következő nap végéig
        self.scheduler.step_day(600)
        self.assertTrue(wait_until(lambda: len(self.day_ends) == 2 and self.scheduler.get_status()["state"] == "paused"))
        self.assertEqual(self.day_ends[1], datetime(2026, 8, 5))
        self.assertEqual(len(self.processor.seen), 19 + 24)
        # a sima folytatás a végéig megy
        self.scheduler.resume()
        self.assertTrue(wait_until(lambda: self.scheduler.get_status()["state"] == "finished"))
        self.assertEqual(len(self.processor.seen), len(FakeQueue().events))


if __name__ == "__main__":
    unittest.main()
