"""Tesztek: model/diagnostics/similar_description.py."""
import unittest

from model.diagnostics.detectors.similar_description import SimilarDescriptionDiagnosis
from tests.support.diagnostic_fixtures import at, measured
from tests.support.profile_fixtures import make_incident


class SimilarDescriptionTest(unittest.TestCase):
    def test_first_description_within_window(self):
        def ticket(incident_id, hours, text, later=None):
            events = [(at(hours), "short_description", "", text)]
            if later:
                events.append((at(hours + 1), "short_description", text, later))
            return make_incident(incident_id, events)
        incidents = [ticket("I1", 0, "VPN nem megy"), ticket("I2", 1, "vpn nem megy  ", later="Megoldva"),
                     ticket("I3", 2, "VPN NEM MEGY"), ticket("I4", -48, "VPN nem megy")]   # I4 az ablakon kívül
        found = measured(SimilarDescriptionDiagnosis(), incidents, 5)
        [issue] = found.values()
        self.assertEqual(issue.affected_objects, ("I1", "I2", "I3"))


if __name__ == "__main__":
    unittest.main()
