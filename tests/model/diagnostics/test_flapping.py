"""Tesztek: model/diagnostics/flapping.py."""
import unittest

from model.diagnostics.detectors.flapping import FlappingDiagnosis
from tests.support.diagnostic_fixtures import at, measured
from tests.support.profile_fixtures import make_incident


class FlappingTest(unittest.TestCase):
    def test_reopen_counted_once_despite_duplicate_state_fields(self):
        events = [(at(0), "state", None, "1")]
        for hour in (2, 4):
            events += [(at(hour), "state", "1", "6"), (at(hour), "incident_state", "1", "6"),
                       (at(hour + 1), "state", "6", "2"), (at(hour + 1), "incident_state", "6", "2")]
        found = measured(FlappingDiagnosis(), [make_incident("INC1", events)], 10)["INC1"]
        self.assertEqual(found.details["transition_count"], 2)
        self.assertEqual(found.issue_id, "ISSUE-FLAPPING_INCIDENT-INC1:FLAPPING_INCIDENT")


if __name__ == "__main__":
    unittest.main()
