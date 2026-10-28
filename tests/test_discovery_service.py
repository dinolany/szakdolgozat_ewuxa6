import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from analyzer import IncidentAnalyzer
from event_processor import IncidentEventProcessor
from models.discovery.service import DiscoveryService
from repository import IncidentRepository


def events_for_day(day: datetime, count: int = 6):
    rows = []
    for index in range(count):
        opened = day + timedelta(hours=9, minutes=10 * index)
        incident = f"INC{day:%d}{index:02d}"
        rows += [
            {"set": incident, "timestamp": opened.isoformat(), "field": "state", "old_value": None, "new_value": "1"},
            {"set": incident, "timestamp": opened.isoformat(), "field": "assignment_group", "old_value": "", "new_value": "L1"},
            {"set": incident, "timestamp": (opened + timedelta(minutes=5)).isoformat(), "field": "assignment_group",
             "old_value": "L1", "new_value": "A" if index % 2 else "B"},
        ]
    return rows


class DiscoveryServiceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        (base / "config").mkdir()
        self.repo = IncidentRepository(str(base / "database.json"))
        self.analyzer = IncidentAnalyzer(str(base / "thresholds.json"), knowledge_path=str(base / "knowledge.json"))
        self.service = DiscoveryService(self.analyzer, self.repo, str(base / "discovery_state.json"),
                                        str(base / "config" / "discovery.json"))
        self.processor = IncidentEventProcessor(self.repo, self.analyzer, discovery=self.service)

    def tearDown(self):
        self.directory.cleanup()

    def last_run(self):
        return (self.service.manager.state["meta"].get("last_run") or {}).get("measured_at")

    def test_runs_once_per_virtual_day_after_batches(self):
        day1, day2 = datetime(2026, 8, 3), datetime(2026, 8, 4)
        rows = events_for_day(day1)
        self.processor.process_events(rows[:9])
        first = self.last_run()
        self.assertIsNotNone(first)
        self.processor.process_events(rows[9:])          # ugyanaz a nap: nem fut újra
        self.assertEqual(self.last_run(), first)
        self.processor.process_events(events_for_day(day2))
        self.assertTrue(self.last_run().startswith("2026-08-04"))

    def test_level_fact_reanalyzes_the_main_database(self):
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)))
        self.service.fact({"op": "add", "predicate": "szint", "args": {"csoport": "L1", "szint": "L1"}})
        self.assertEqual(dict(self.analyzer.knowledge.view().levels), {"L1": "L1"})
        self.assertTrue(Path(self.directory.name, "knowledge.json").exists())

    def test_time_jumping_back_restarts_discovery(self):
        self.processor.process_events(events_for_day(datetime(2026, 8, 10)))
        self.assertTrue(self.last_run().startswith("2026-08-10"))
        self.service.manager.state["meta"]["marker"] = "régi lejátszás"
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)))   # új lejátszás reset nélkül
        self.assertTrue(self.last_run().startswith("2026-08-03"))
        self.assertNotIn("marker", self.service.manager.state["meta"])

    def test_analysis_flags_candidate_not_covered_by_current_data(self):
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)) + events_for_day(datetime(2026, 8, 4)))
        candidate = next(iter(self.service.manager.candidates()), None)
        if candidate is None:
            self.skipTest("a kis adatból nem lett jelölt")
        candidate.window_end = "2026-08-20T00:00:00"                        # egy későbbi mérésből
        self.service.manager._save_candidate(candidate)
        self.assertTrue(self.service.analysis(candidate.candidate_id)["stale"])

    def test_reset_keeps_knowledge(self):
        self.service.fact({"op": "add", "predicate": "szint", "args": {"csoport": "L1", "szint": "L1"}})
        self.processor.process_events(events_for_day(datetime(2026, 8, 3)))
        self.service.reset()
        self.assertIsNone(self.last_run())
        self.assertEqual(len(self.service.knowledge_overview()["facts"]), 1)


if __name__ == "__main__":
    unittest.main()
