import unittest

from learning.generalize import suggestions
from learning.knowledge import Rule


def rule(rule_id, conditions, measures=None, source="decision:DEC-1"):
    return Rule(rule_id, "rejection", "drop", [{"dim": d, "values": [v]} for d, v in conditions],
                measure_filter={"mode": "list", "measures": measures} if measures else {"mode": "any"}, source=source)


ROLES = {"subcategory": 2, "assignment_group": 2, "business_service": 3}


class GeneralizeTest(unittest.TestCase):
    def test_repeated_value_in_rules_suggests_general_rule(self):
        rules = [rule("R1", [("subcategory", "1"), ("weekday", "Mon")], ["stint_pickup"]),
                 rule("R2", [("subcategory", "1"), ("weekday", "Tue")], ["stint_hold"]),
                 rule("R3", [("subcategory", "1"), ("assignment_group", "G")], ["stint_pickup"])]
        found = {s["key"]: s for s in suggestions(rules, {}, ROLES, {}, 3)}
        general = found["general_rule|subcategory=1"]
        self.assertEqual((general["measures"], general["rules"]), (["stint_hold", "stint_pickup"], ["R1", "R2", "R3"]))
        self.assertNotIn("dimension_role|assignment_group", found)   # entitás-mezőre nincs mező-szintű javaslat
        # ha már van ilyen általános szabály, nem javasolja újra
        rules.append(rule("R4", [("subcategory", "1")], source="generalization"))
        self.assertNotIn("general_rule|subcategory=1", {s["key"] for s in suggestions(rules, {}, ROLES, {}, 3)})

    def test_attribute_dimension_with_many_values_suggests_narrowing(self):
        rules = [rule(f"R{i}", [("subcategory", str(i)), ("assignment_group", f"G{i}")]) for i in range(3)]
        found = {s["key"] for s in suggestions(rules, {}, ROLES, {}, 3)}
        self.assertIn("dimension_role|subcategory", found)

    def test_breakdown_dead_ends_and_good_paths(self):
        store = {"dimensions": {"business_service": {"good": 3, "hidden": False}, "subcategory": {"good": 3}},
                 "log": [{"verdict": "dead_end", "dimension": "current_group", "parent": p} for p in ("P1", "P2", "P2", "P3")]}
        found = {s["key"]: s for s in suggestions([], store, ROLES, {}, 3)}
        self.assertEqual(found["breakdown_never|current_group"]["evidence"], 3)   # különböző szülők száma
        self.assertIn("emphasize|subcategory", found)
        self.assertNotIn("emphasize|business_service", found)                     # már kiemelt

    def test_dismissed_comes_back_only_with_new_evidence(self):
        store = {"log": [{"verdict": "dead_end", "dimension": "hour", "parent": f"P{i}"} for i in range(4)]}
        self.assertEqual(suggestions([], store, ROLES, {"breakdown_never|hour": 3}, 3), [])
        store["log"] += [{"verdict": "dead_end", "dimension": "hour", "parent": f"Q{i}"} for i in range(2)]
        self.assertEqual([s["key"] for s in suggestions([], store, ROLES, {"breakdown_never|hour": 3}, 3)], ["breakdown_never|hour"])


if __name__ == "__main__":
    unittest.main()


class BreakdownPathTest(unittest.TestCase):
    def test_day_then_subcategory_is_learned_across_groups(self):
        log = [{"verdict": "dead_end", "dimension": "subcategory", "parent": "P1", "parent_dims": ["weekday"]},
               {"verdict": "dead_end", "dimension": "subcategory", "parent": "P2", "parent_dims": ["assignment_group", "weekday"]},
               {"verdict": "dead_end", "dimension": "subcategory", "parent": "P3", "parent_dims": ["business_service", "date"]}]
        found = {s["key"]: s for s in suggestions([], {"log": log}, ROLES, {}, 3)}
        path = found["breakdown_path|nap>subcategory"]      # hét napja és dátum ugyanaz a fajta: nap
        self.assertEqual((path["parent_class"], path["dimension"], path["evidence"]), ("nap", "subcategory", 3))
        self.assertNotIn("breakdown_path|csoport>subcategory", found)   # csak 1 szülőnél volt csoport
        learned = suggestions([], {"log": log, "paths": {"nap>subcategory": {"blocked": True}}}, ROLES, {}, 3)
        self.assertNotIn("breakdown_path|nap>subcategory", [s["key"] for s in learned])   # megtanulva: nem javasolja újra

    def test_blocked_path_in_mining_and_matching(self):
        import random
        from datetime import datetime, timedelta
        from learning import MiningParams, mine
        from learning.mining import path_blocked
        from tests.test_discovery import PICKUP, make_profile
        self.assertTrue(path_blocked({"weekday", "assignment_group"}, "subcategory", [("nap", "subcategory")]))
        self.assertTrue(path_blocked({"date"}, "subcategory", [("nap", "subcategory")]))
        self.assertFalse(path_blocked({"business_service"}, "subcategory", [("nap", "subcategory")]))
        rng = random.Random(4)
        profiles = []
        for i in range(3000):
            opened = datetime(2026, 8, 3) + timedelta(days=rng.randrange(28), hours=rng.randrange(8, 18))
            sat, sub = opened.weekday() == 5, f"{rng.randrange(1, 4)}"
            rate = (0.95 if sub == "1" else 0.3) if sat else 0.04
            profiles.append(make_profile(i, opened, f"G{rng.randrange(1, 6)}", "S1", rng.random() < rate, {"subcategory": sub}))
        saturday = (("weekday", "Sat"),)
        children = lambda params: [r.items for r in mine(profiles, PICKUP, params, contexts=[saturday]) if r.extra]
        self.assertIn((("subcategory", "1"), ("weekday", "Sat")), children(MiningParams(min_support=3, fdr_q=0.05)))
        blocked = MiningParams(min_support=3, fdr_q=0.05, blocked_paths=frozenset({("nap", "subcategory")}))
        self.assertFalse(any(d == "subcategory" for items in children(blocked) for d, _ in items))
