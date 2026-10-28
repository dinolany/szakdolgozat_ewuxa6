"""Tesztek: learning/feedback/learned_thresholds.py."""
from datetime import timedelta
import unittest

from tests.support.manager_fixtures import DAY


class LearnedThresholdTest(unittest.TestCase):
    def test_suggests_where_exceeding_is_the_norm(self):
        from learning.feedback.learned_thresholds import suggestions
        base = {"moderate_moves": 3, "extreme_moves": 5}
        busy = [((("business_service", "S"),), v) for v in (3, 4, 4, 5, 3, 6, 4, 2, 3, 7)]      # a jegyek többsége >= 3
        calm = [((("business_service", "Q"),), v) for v in (0, 1, 0, 3, 1, 0, 2, 0, 1, 0)]
        found = suggestions("PING_PONG", "Ping-pong", base, busy + calm)
        self.assertEqual([s["scope"] for s in found], [[["business_service", "S"]]])
        self.assertEqual(found[0]["params"], {"moderate_moves": 6, "extreme_moves": 8})   # 90. percentilis, az extrém +2
        self.assertEqual(suggestions("PING_PONG", "Ping-pong", base, busy, existing_scopes=[(("business_service", "S"),)]), [])
        self.assertEqual(suggestions("PING_PONG", "Ping-pong", base, busy, dismissed={"PING_PONG|business_service=S": "6"}), [])
        # friss túllépés (minden túllépő jegy egy héten belül): lehet, hogy hiba, ezért nincs javaslat
        recent = [(items, v, DAY + timedelta(days=i % 5)) for i, (items, v) in enumerate(busy)]
        lasting = [(items, v, DAY + timedelta(days=3 * i)) for i, (items, v) in enumerate(busy)]
        self.assertEqual(suggestions("PING_PONG", "Ping-pong", base, recent), [])
        self.assertEqual(len(suggestions("PING_PONG", "Ping-pong", base, lasting)), 1)


if __name__ == "__main__":
    unittest.main()
