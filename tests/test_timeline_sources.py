import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from simulation.timeline_selector import TimelineSelector


class FakeTransformer:
    def __init__(self):
        self.cleared = 0

    def get_timeline(self):
        return [{"_virtual_dt": datetime(2026, 8, 1), "id": "real"}]

    def clear_cache(self):
        self.cleared += 1


class TimelineSelectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.transformer = FakeTransformer()

    def tearDown(self):
        self.tmp.cleanup()

    def selector(self):
        return TimelineSelector(self.transformer, base_dir=self.base)

    def test_missing_synthetic_is_reported_not_selected(self):
        selector = self.selector()
        self.assertEqual(selector.source, "real")
        synthetic = next(s for s in selector.available() if s["name"] == "synthetic")
        self.assertFalse(synthetic["available"])
        self.assertIn("simulation.synthetic.build_synthetic", synthetic["hint"])
        with self.assertRaises(ValueError):
            selector.select("synthetic")

    def test_synthetic_source_loads_sorted_and_survives_reset(self):
        rows = [{"_virtual_dt": "2026-05-10T10:00:00", "id": "b"}, {"_virtual_dt": "2026-05-09T09:00:00", "id": "a"}]
        (self.base / "synthetic_timeline.json").write_text(json.dumps({"meta": {}, "rows": rows}), encoding="utf-8")
        (self.base / "synthetic_timeline.faults.json").write_text(json.dumps(
            {"scenarios": [{"name": "x", "kind": "ping_pong", "group": "G", "start": "2026-06-01", "end": "2026-06-08",
                            "shape": "step", "applied": 4, "secret": 1}], "l1": "L1"}), encoding="utf-8")
        selector = self.selector()
        selector.select("synthetic")
        timeline = selector.get_timeline()
        self.assertEqual([r["id"] for r in timeline], ["a", "b"])
        self.assertIsInstance(timeline[0]["_virtual_dt"], datetime)
        selector.clear_cache()   # reset: a szintetikus fájl megmarad, a valódi gyorsítótár nem ürül
        self.assertTrue((self.base / "synthetic_timeline.json").exists())
        self.assertEqual(self.transformer.cleared, 0)
        status = selector.status()
        self.assertEqual(status["faults"]["scenarios"][0]["name"], "x")
        self.assertNotIn("secret", status["faults"]["scenarios"][0])
        self.assertEqual(self.selector().source, "synthetic")   # a választás megmarad
        selector.select("real")
        selector.clear_cache()
        self.assertEqual(self.transformer.cleared, 1)
        self.assertIsNone(selector.status()["faults"])


if __name__ == "__main__":
    unittest.main()
