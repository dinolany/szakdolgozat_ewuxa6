"""Tesztek: model/diagnostics/unassigned.py."""
import unittest

from model.diagnostics.detectors.unassigned import UnassignedDiagnosis
from tests.support.diagnostic_fixtures import at, measured
from tests.support.profile_fixtures import make_incident


class UnassignedTest(unittest.TestCase):
    def test_open_ticket_without_group_as_of_measurement(self):
        incident = make_incident("INC1", [
            (at(0), "state", None, "1"),
            (at(0), "assignment_group", "", "A"),
            (at(2), "assignment_group", "A", ""),   # a csoportot törölték
            (at(10), "assignment_group", "", "B"),
        ])
        self.assertEqual(measured(UnassignedDiagnosis(), [incident], 1), {})
        found = measured(UnassignedDiagnosis(), [incident], 7)["INC1"]
        self.assertEqual((found.details["hours"], str(found.severity)), (5.0, "EXCEEDED_EXTREME"))
        self.assertEqual(found.issue_id, "ISSUE-UNASSIGNED_TICKET-INC1:UNASSIGNED_TICKET")
        self.assertEqual(measured(UnassignedDiagnosis(), [incident], 11), {})  # azóta kapott csoportot


if __name__ == "__main__":
    unittest.main()
