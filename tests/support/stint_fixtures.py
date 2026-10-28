"""Közös tesztadatok és -segédek.

Használja: learning/mining/test_contrast_mining.py, learning/mining/test_profiles.py, learning/test_discovery_manager.py, learning/test_pattern_finding_diagnosis.py."""
from datetime import datetime, timedelta

from learning import MEASURES_BY_KEY
from tests.support.profile_fixtures import make_incident


START = datetime(2026, 8, 3)  # hétfő

LEVELS = {"L1": "L1"}

STINT_PICKUP = MEASURES_BY_KEY["stint_pickup"]

def dispatched(incident_id, opened, target, pickup_hours, service="S1", later=None):
    """L1 nyitja, 10 perc múlva továbbítja `target`-nek, ott `pickup_hours` múlva veszik fel."""
    arrival = opened + timedelta(minutes=10)
    events = [
        (opened.isoformat(), "state", None, "1"),
        (opened.isoformat(), "assignment_group", "", "L1"),
        (opened.isoformat(), "business_service", "", service),
        (opened.isoformat(), "assigned_to", "", "agent"),
        (arrival.isoformat(), "assignment_group", "L1", target),
        (arrival.isoformat(), "assigned_to", "agent", ""),
        ((arrival + timedelta(hours=pickup_hours)).isoformat(), "assigned_to", "", "resolver"),
    ]
    if later:
        moment = arrival + timedelta(hours=pickup_hours + 1)
        events.append((moment.isoformat(), "assignment_group", target, later))
    return make_incident(incident_id, events)

def week():
    """Két hét: az A csoport szombati érkezéseit 20 óra múlva veszik fel, minden mást 1 óra alatt."""
    incidents = []
    for index in range(140):
        day, target = index % 14, ("A" if (index // 14) % 2 == 0 else "B")  # naponta 5 A és 5 B
        opened = START + timedelta(days=day, hours=8 + index % 6)
        slow = target == "A" and opened.weekday() == 5
        incidents.append(dispatched(f"INC{index:03d}", opened, target, 20 if slow else 1))
    return incidents
