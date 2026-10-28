import unittest
from datetime import datetime, timedelta

from learning.knowledge import Rule
from learning.review import ReviewParams, review_facts, review_rules
from model.knowledge.knowledge_base import Fact
from model.timeline.ticket_timeline import incident_timeline
from tests.test_discovery import make_incident

NOW = datetime(2026, 9, 30)


def sunday_work(incident_id, sunday):
    """A G csoport vasárnap felveszi és lezárja a ticketet."""
    t = lambda hours: (sunday + timedelta(hours=hours)).isoformat()
    return make_incident(incident_id, [
        (t(8), "state", None, "1"),
        (t(8), "assignment_group", "", "G"),
        (t(9), "assigned_to", "", "u1"),
        (t(10), "state", "1", "6"),
    ])


class RuleReviewTest(unittest.TestCase):
    def test_never_hit_stale_and_expired(self):
        rules = [
            Rule("RULE-1", "rejection", "drop", created_at="2026-09-01T00:00:00"),                       # soha nem talált
            Rule("RULE-2", "rejection", "drop", created_at="2026-07-01T00:00:00", hits=5,
                 last_hit_at="2026-08-01T00:00:00"),                                                     # rég nem talált
            Rule("RULE-3", "rejection", "drop", created_at="2026-07-01T00:00:00", valid_until="2026-09-01"),
            Rule("RULE-4", "rejection", "drop", created_at="2026-09-25T00:00:00"),                       # még fiatal
            Rule("RULE-5", "rejection", "drop", created_at="2026-07-01T00:00:00", reviewed_at="2026-09-28T00:00:00"),
        ]
        items = {item.object_id: item.reason for item in review_rules(rules, NOW)}
        self.assertEqual(items, {"RULE-1": "never_hit", "RULE-2": "stale", "RULE-3": "expired"})


class FactReviewTest(unittest.TestCase):
    def setUp(self):
        sundays = [datetime(2026, 9, 6), datetime(2026, 9, 13), datetime(2026, 9, 20), datetime(2026, 9, 27)]
        self.timelines = [incident_timeline(sunday_work(f"INC{i}", day), NOW) for i, day in enumerate(sundays)]

    def test_not_working_contradicted_by_data(self):
        fact = Fact("FACT-1", "nem_dolgozik", {"csoport": "G", "nap": "Sun"}, created_at="2026-09-01T00:00:00")
        [item] = review_facts([fact], self.timelines, NOW)
        self.assertEqual((item.reason, item.evidence), ("contradicted", 8))  # felvétel + lezárás, 4 vasárnap

    def test_fact_added_today_is_checked_against_past_data(self):
        fact = Fact("FACT-1", "nem_dolgozik", {"csoport": "G", "nap": "Sun"}, created_at="2026-09-29T00:00:00")
        self.assertEqual([i.reason for i in review_facts([fact], self.timelines, NOW)], ["contradicted"])

    def test_confirmation_silences_earlier_evidence(self):
        fact = Fact("FACT-1", "nem_dolgozik", {"csoport": "G", "nap": "Sun"}, created_at="2026-09-01T00:00:00",
                    reviewed_at="2026-09-21T00:00:00")
        self.assertEqual(review_facts([fact], self.timelines, NOW, ReviewParams(min_contradictions=3)), [])

    def test_unseen_level_group_and_unconfirmed(self):
        facts = [Fact("FACT-1", "szint", {"csoport": "X", "szint": "L1"}, created_at="2026-09-01T00:00:00"),
                 Fact("FACT-2", "szint", {"csoport": "G", "szint": "L1"}, created_at="2026-05-01T00:00:00")]
        items = {item.object_id: item.reason for item in review_facts(facts, self.timelines, NOW)}
        self.assertEqual(items, {"FACT-1": "unseen", "FACT-2": "unconfirmed"})

    def test_unavailable_feature_used_anyway(self):
        fact = Fact("FACT-1", "nem_elerheto", {"funkcio": "assigned_to", "csoport": "G"}, created_at="2026-09-01T00:00:00")
        [item] = review_facts([fact], self.timelines, NOW)
        self.assertEqual((item.reason, item.evidence), ("contradicted", 4))


if __name__ == "__main__":
    unittest.main()
