import unittest
from collections import Counter
from datetime import datetime, timedelta

from simulation.synthetic.compare import incidents_from_rows, window_summary
from simulation.synthetic.generator import GeneratorParams, build_templates, generate, instantiate, merge_timelines

START = datetime(2026, 8, 3)  # hétfő


def row(incident_id, moment, field, old, new):
    stamp = moment.isoformat()
    return {"set": incident_id, "field": field, "old_value": old, "new_value": new,
            "timestamp": stamp, "update_time": stamp, "sys_created_on": stamp, "_virtual_dt": stamp}


def lifecycle(incident_id, opened, group, service, pickup_hours, moves=()):
    rows = [row(incident_id, opened, "state", None, "1"),
            row(incident_id, opened, "assignment_group", "", group),
            row(incident_id, opened, "business_service", "", service)]
    previous = group
    for index, target in enumerate(moves, 1):
        rows.append(row(incident_id, opened + timedelta(minutes=10 * index), "assignment_group", previous, target))
        previous = target
    rows.append(row(incident_id, opened + timedelta(hours=pickup_hours), "assigned_to", "", "U1"))
    return rows


def sample_rows():
    rows = []
    for week in range(2):
        for weekday in range(7):
            opened = START + timedelta(days=7 * week + weekday, hours=9 + weekday)
            rows += lifecycle(f"A{week}{weekday}", opened, "G1", "S1", 1)
            rows += lifecycle(f"B{week}{weekday}", opened + timedelta(minutes=5), "G1", "S2", 30, moves=("G2", "G3"))
            rows += lifecycle(f"C{week}{weekday}", opened + timedelta(minutes=7), "G9", "S9", 2)
    return rows


class TemplateTest(unittest.TestCase):
    def test_offsets_and_first_values(self):
        templates = {t.incident_id: t for t in build_templates(sample_rows())}
        b = templates["B00"]
        self.assertEqual(b.first_values, {"assignment_group": "G1", "business_service": "S2"})
        self.assertEqual(b.events[0][0], 0.0)
        self.assertEqual(b.events[-1][0], 30 * 3600)
        self.assertNotIn("timestamp", b.events[0][1])

    def test_swapped_lifecycle_keeps_template_dimensions(self):
        templates = {t.incident_id: t for t in build_templates(sample_rows())}
        opened = datetime(2026, 7, 6, 9)
        rows = instantiate(templates["A00"], templates["B00"], "SYN1", opened, datetime(2026, 8, 15))
        services = [r["new_value"] for r in rows if r["field"] == "business_service"]
        self.assertEqual(services, ["S1"])                  # a sablon service-e
        moves = [(r["old_value"], r["new_value"]) for r in rows if r["field"] == "assignment_group"]
        self.assertEqual(moves, [("", "G1"), ("G1", "G2"), ("G2", "G3")])  # a donor lefutása
        self.assertEqual({r["set"] for r in rows}, {"SYN1"})
        self.assertEqual(rows[-1]["timestamp"], (opened + timedelta(hours=30)).isoformat())

    def test_cut_at_drops_later_events(self):
        templates = {t.incident_id: t for t in build_templates(sample_rows())}
        opened = datetime(2026, 8, 14, 20)
        rows = instantiate(templates["B00"], templates["B00"], "SYN1", opened, datetime(2026, 8, 15))
        self.assertNotIn("assigned_to", [r["field"] for r in rows])


class GenerateTest(unittest.TestCase):
    def setUp(self):
        self.templates = build_templates(sample_rows())
        self.start = datetime(2026, 5, 9)  # szombat, mint a virtuális kezdet

    def test_deterministic_and_volume(self):
        params = GeneratorParams(weeks=8, seed=3)
        rows1, summary1 = generate(self.templates, self.start, datetime(2026, 8, 15), params)
        rows2, _ = generate(self.templates, self.start, datetime(2026, 8, 15), params)
        self.assertEqual(rows1, rows2)
        # sablonból naponta 3 ticket (2 hét alatt 6 hétnaponként): 8 hét alatt kb. 168
        self.assertTrue(120 < summary1["incidents"] < 220, summary1)

    def test_weekday_and_group_structure_preserved(self):
        rows, _ = generate(self.templates, self.start, datetime(2026, 8, 15), GeneratorParams(weeks=8, seed=3))
        opened = {}
        for r in rows:
            opened.setdefault(r["set"], (datetime.fromisoformat(r["timestamp"]), r["_template"]))
        for moment, template_id in opened.values():
            template = next(t for t in self.templates if t.incident_id == template_id)
            self.assertEqual(moment.weekday(), template.weekday)
        # G9 sosem kap G1-es lefutást (a csere csoporton belül marad)
        lifecycles = Counter((r["_template"][0], r["_lifecycle"][0]) for r in rows if r["field"] == "state")
        self.assertFalse([pair for pair in lifecycles if "C" in pair and pair != ("C", "C")])

    def test_merged_timeline_is_measurable(self):
        synthetic, _ = generate(self.templates, self.start, datetime(2026, 8, 15), GeneratorParams(weeks=4, seed=1))
        merged = merge_timelines(synthetic, sample_rows())
        stamps = [r["_virtual_dt"] for r in merged]
        self.assertEqual(stamps, sorted(stamps))
        summary = window_summary(incidents_from_rows(merged), datetime(2026, 5, 23), datetime(2026, 6, 5, 23, 59))
        self.assertGreater(summary["incidents"], 0)
        self.assertEqual(set(summary["groups"]), {"G1", "G9"})


if __name__ == "__main__":
    unittest.main()
