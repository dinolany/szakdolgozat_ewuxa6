import unittest
from datetime import datetime, timedelta

from learning.analysis import analyze_candidate, brief, describe_condition
from learning.knowledge import Candidate, make_candidate_id
from tests.test_discovery import make_incident

START = datetime(2026, 8, 3, 9)   # hétfő


def ticket(incident_id, opened, group, pickup_hours):
    return make_incident(incident_id, [
        (opened.isoformat(), "state", None, "1"),
        (opened.isoformat(), "assignment_group", "", group),
        ((opened + timedelta(hours=pickup_hours)).isoformat(), "assigned_to", "", "u1"),
    ])


def incidents():
    result = []
    for day in range(7):
        for index in range(4):
            opened = START + timedelta(days=day, hours=index)
            saturday = opened.weekday() == 5
            group = "L1" if index < 3 else "X"
            # szombaton az L1 ticketjeit csak hétfőn veszik fel
            result.append(ticket(f"I{day}{index}", opened, group, 45 if saturday and group == "L1" else 0.5))
    return result


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        key = "weekday=Sat"
        self.candidate = Candidate(make_candidate_id("pickup_delay", key), "pickup_delay", key, window_end="2026-08-10T00:00:00")
        self.result = analyze_candidate(self.candidate, incidents(), datetime(2026, 8, 10), 7, 8)

    def test_summary_and_conditions(self):
        self.assertEqual(self.result["conditions"], ["a ticket nyitásának napja: szombat"])
        stats = self.result["stats"]
        self.assertEqual((stats["pattern"], stats["pattern_bad"], stats["population"]), (4, 3, 28))
        self.assertIn("4 ticket illik a mintára", self.result["summary"])

    def test_verdict_names_the_common_point_or_says_there_is_none(self):
        verdict = self.result["verdict"]
        self.assertIn("pending_share", verdict)
        self.assertTrue(verdict["text"].startswith("Közös pont") or verdict["text"].startswith("Nincs közös pont"))

    def test_common_point_is_the_group(self):
        group = next(c for c in self.result["common"] if c["dimension"] == "assignment_group")
        self.assertEqual((group["value"], group["share_in_bad"]), ("L1", 1.0))
        self.assertAlmostEqual(group["share_in_population"], 0.75)
        self.assertEqual((self.result["weekday"], self.result["weekday_text"]), ("Sat", "szombat"))

    def test_units_trend_and_histogram(self):
        pattern_units = [u for u in self.result["units"] if u["in_pattern"]]
        self.assertEqual(len(pattern_units), 4)
        # a mérés pillanatáig felvett ticketnél ott a felvétel eseménye; a még várónál nincs (az érték az eddigi várakozás)
        self.assertEqual(sorted(bool(u["evidence"]) for u in pattern_units), [False, False, False, True])
        self.assertEqual(self.result["trend"][0]["day"], "2026-08-03")        # az adat első napjától
        saturday = next(d for d in self.result["trend"] if d["day"] == "2026-08-08")
        self.assertEqual((saturday["pattern"], saturday["pattern_bad"]), (4, 3))
        self.assertEqual(sum(h["pattern"] for h in self.result["histogram"]), 4)

    def test_each_case_says_why_it_is_affected(self):
        self.assertIn("4 ticket illik a mintára az ablakban, ebből 3 érintett", self.result["summary"])
        units = {u["incident_id"]: u for u in self.result["units"]}
        waiting = next(u for u in units.values() if u["in_pattern"] and u["pending"])
        self.assertTrue(waiting["reason"].startswith("még nincs felelőse:"))
        self.assertIn("küszöb: 8 óra", waiting["reason"])
        fine = next(u for u in units.values() if not u["bad"])
        self.assertTrue(fine["reason"].endswith("(rendben)"))

    def test_brief_for_the_queue_card(self):
        card = brief(self.result)
        self.assertEqual(card["conditions"], ["a ticket nyitásának napja: szombat"])
        self.assertTrue(card["verdict"])
        self.assertEqual(len(card["examples"]), 3)                 # a 3 érintett eset, a legkiugróbbtól
        self.assertTrue(all(e["reason"] for e in card["examples"]))

    def test_stint_conditions_are_worded_by_arrival(self):
        self.assertEqual(describe_condition("weekday", "Sun", "stint"), "a csoporthoz érkezés napja: vasárnap")
        self.assertEqual(describe_condition("assignment_group", "G", "ticket"), "a ticket útjában szerepel a(z) G megoldócsoport")
        self.assertEqual(describe_condition("assignment_group", "G", "stint"),
                         "csoport a mintavételkor (akinél a szakasz alatt volt): G")


if __name__ == "__main__":
    unittest.main()


class BreakdownTest(unittest.TestCase):
    def test_half_subcategory_half_service(self):
        from datetime import datetime, timedelta
        from learning.analysis import breakdown
        from learning.knowledge import Candidate
        from learning.profile import ProfileSet
        from tests.test_discovery import make_profile
        wed = datetime(2026, 8, 5, 10)   # szerda
        profiles, index = [], 0
        def add(n, bad, extra, service="S0"):
            nonlocal index
            for _ in range(n):
                profiles.append(make_profile(index, wed, "G1", service, bad, extra)); index += 1
        add(4, True, {"subcategory": "1"})            # a lassúak fele 1-es alkategória
        add(4, True, {"subcategory": "7"}, "SX")      # a másik fele az SX service
        add(30, False, {"subcategory": "7"})          # a nem lassú szerdaiak
        candidate = Candidate("C", "pickup_delay", "weekday=Wed")
        result = breakdown(candidate, ProfileSet(tuple(profiles), ()), 8.0, wed - timedelta(days=1))
        found = {(r["dimension"], r["value"]): r for r in result["rows"]}
        self.assertEqual((result["units"], result["bad"]), (38, 8))
        self.assertAlmostEqual(found[("subcategory", "1")]["share_in_bad"], 0.5)
        self.assertAlmostEqual(found[("business_service", "SX")]["share_in_bad"], 0.5)
        self.assertAlmostEqual(found[("business_service", "SX")]["share_in_pattern"], 4 / 38)
