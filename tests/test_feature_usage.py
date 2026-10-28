import unittest
from datetime import datetime, timedelta

from model.diagnostics.core import MeasurementContext, Trigger
import json
import tempfile
from pathlib import Path

from model.diagnostics.detectors.feature_usage import FeatureDef, FeatureUsageDiagnosis, load_catalog, occasions
from model.knowledge.knowledge_base import KnowledgeView
from model.timeline.ticket_timeline import incident_timeline
from tests.test_discovery import make_incident

START = datetime(2026, 8, 3, 9)
AS_OF = datetime(2026, 8, 20)


def ticket(incident_id, opened, target, with_reason, routed=True, flagged=False):
    """L1 nyitja (létrehozáskori alapértékekkel), továbbítja `target`-nek, ott holdba teszik és lezárják."""
    t = lambda minutes: (opened + timedelta(minutes=minutes)).isoformat()
    events = [
        (t(0), "state", None, "1"),
        (t(0), "assignment_group", "", "L1"),
        (t(0), "business_service", "", "S1"),
        (t(0), "u_wrong_assignment_flag", None, "0"),     # létrehozáskori alapérték: nem használat
        (t(5), "assigned_to", "", "agent"),
        (t(10), "assignment_group", "L1", target),
        (t(10), "assigned_to", "agent", ""),
        (t(60), "assigned_to", "", "resolver"),
        (t(70), "state", "1", "3"),
        (t(70), "incident_state", "1", "3"),               # duplikátum: egy hold
        (t(300), "state", "3", "2"),
        (t(400), "state", "2", "6"),
        (t(400), "close_code", "", "13"),
    ]
    if routed:
        events.append((t(10), "u_routing_instructions", None, "R1"))   # a továbbítás mentésében: az L1-é
    if with_reason:
        events.append((t(71), "u_pending_reason", None, "100"))
    if flagged:
        events.append((t(30), "u_wrong_assignment_flag", "0", "1"))    # a fogadó jelöli: az L1 rossz továbbítása
    return make_incident(incident_id, events)


def incidents(a_reason=False, routed=True, flagged_count=0):
    result = []
    for index in range(12):
        target = "A" if index % 2 == 0 else "B"
        opened = START + timedelta(hours=6 * index)
        result.append(ticket(f"INC{index:02d}", opened, target, with_reason=(target == "B" or a_reason),
                             routed=routed, flagged=index < flagged_count))
    return result


class OccasionTest(unittest.TestCase):
    def test_attribution_and_defaults(self):
        timeline = incident_timeline(ticket("INC1", START, "A", with_reason=False, flagged=True), AS_OF)
        by_feature = {}
        for occasion in occasions(timeline, {"hold_tolerance_minutes": 30}, levels={"L1": "L1"}):
            by_feature.setdefault(occasion.feature, []).append((occasion.group, occasion.used))
        self.assertEqual(by_feature["hold_reason"], [("A", False)])             # egy hold, a duplikátum nem kettő
        self.assertEqual(by_feature["routing_instructions"], [("L1", True)])    # a küldő mentése
        self.assertEqual(by_feature["close_code"], [("A", True)])
        self.assertEqual(by_feature["wrong_assignment"], [("L1", True)])        # a fogadó jelölte az L1-et
        self.assertEqual(sorted(by_feature["assigned_to"]), [("A", True), ("L1", True)])

    def test_l1_only_feature_needs_level_knowledge(self):
        timeline = incident_timeline(ticket("INC1", START, "A", with_reason=False), AS_OF)
        self.assertNotIn("routing_instructions", {o.feature for o in occasions(timeline, {})})
        routed = [o for o in occasions(timeline, {}, levels={"L1": "L1"}) if o.feature == "routing_instructions"]
        self.assertEqual([o.group for o in routed], ["L1"])      # rezolvernél nem alkalom

    def test_catalog_file_adds_overrides_and_disables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps([
                {"key": "comments", "label": "Ügyfélkommunikáció", "fields": ["comments"], "kind": "on_work"},
                {"key": "close_code", "label": "Lezárási kód (új)", "fields": ["close_code"], "kind": "on_close"},
                {"key": "service_rewritten", "disabled": True},
            ]), encoding="utf-8")
            catalog = {f.key: f for f in load_catalog(path)}
        self.assertIn("comments", catalog)
        self.assertEqual(catalog["close_code"].label, "Lezárási kód (új)")
        self.assertNotIn("service_rewritten", catalog)
        with self.assertRaises(ValueError):
            FeatureDef.from_dict({"key": "x", "fields": ["x"], "kind": "nincs_ilyen"})

    def test_creation_default_is_not_a_hit(self):
        timeline = incident_timeline(ticket("INC1", START, "A", with_reason=True), AS_OF)
        wrong = [o for o in occasions(timeline, {}) if o.feature == "wrong_assignment"]
        self.assertEqual([o.used for o in wrong], [False])


class DiagnosisTest(unittest.TestCase):
    def run_diagnosis(self, items, availability=None):
        knowledge = KnowledgeView(availability=availability or {})
        run = FeatureUsageDiagnosis().run(MeasurementContext(Trigger.MANUAL, AS_OF, items, [], knowledge=knowledge))
        return {m.subject: m for m in run.measurements if m.exceeded}

    def test_never_used_with_unknown_availability_is_a_question(self):
        found = self.run_diagnosis(incidents())
        a = found["hold_reason|A"]
        self.assertEqual((a.details["occasions"], a.details["used"], a.details["availability"]), (6, 0, "unknown"))
        self.assertEqual(str(a.severity), "EXCEEDED_MODERATE")
        self.assertIn("Elérhető nekik?", a.description)
        self.assertEqual(a.details["groups_using"], 1)                 # a B használja
        self.assertNotIn("hold_reason|B", found)
        self.assertEqual(len(a.affected_objects), 6)

    def test_confirmed_availability_makes_it_an_education_need(self):
        found = self.run_diagnosis(incidents(), {("hold_reason", "A"): True})
        a = found["hold_reason|A"]
        self.assertEqual(str(a.severity), "EXCEEDED_EXTREME")
        self.assertIn("edukáció", a.description)

    def test_unavailable_feature_is_not_flagged(self):
        found = self.run_diagnosis(incidents(), {("hold_reason", "A"): False})
        self.assertNotIn("hold_reason|A", found)

    def test_feature_nobody_uses_is_one_question(self):
        found = self.run_diagnosis(incidents())       # a hold várható végét senki nem tölti
        self.assertIn("hold_end_date|*", found)
        self.assertNotIn("hold_end_date|A", found)
        self.assertIn("egyetlen csoport sem", found["hold_end_date|*"].description)

    def test_negative_feature_reports_rate(self):
        found = self.run_diagnosis(incidents(flagged_count=4))
        wrong = found["wrong_assignment|L1"]
        self.assertEqual((wrong.details["occasions"], wrong.details["used"]), (12, 4))
        self.assertEqual(str(wrong.severity), "EXCEEDED_MODERATE")
        self.assertNotIn("wrong_assignment|L1", self.run_diagnosis(incidents(flagged_count=1)))


if __name__ == "__main__":
    unittest.main()
