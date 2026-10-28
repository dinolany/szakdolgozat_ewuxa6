"""Közös tesztadatok és -segédek.

Használja: learning/test_discovery_manager.py, learning/test_pattern_finding_diagnosis.py."""
from datetime import datetime, timedelta

from learning.state.candidate import Candidate, CandidateState, make_candidate_id
from tests.support.profile_fixtures import make_incident


START = datetime(2026, 8, 3)  # hétfő

def week_of_incidents():
    """Egy hét: a G csoport vasárnapi ticketjeit 12 óra múlva veszik fel, minden mást 1 óra alatt."""
    incidents = []
    for index in range(42):
        day = index % 7
        group = "G" if index % 2 == 0 else "H"
        opened = START + timedelta(days=day, hours=9 + index % 5)
        slow = group == "G" and day == 6
        assigned = opened + timedelta(hours=12 if slow else 1)
        incidents.append(make_incident(f"INC{index:03d}", [
            (opened.isoformat(), "state", None, "5"),
            (opened.isoformat(), "assignment_group", "", group),
            (assigned.isoformat(), "assigned_to", "", "U1"),
        ]))
    return incidents

def candidate(key, measure="pickup_delay"):
    return Candidate(make_candidate_id(measure, key), measure, key, state=CandidateState.ACCEPTED.value)
