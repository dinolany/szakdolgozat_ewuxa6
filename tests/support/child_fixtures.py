"""Közös tesztadatok és -segédek.

Használja: learning/children/test_child_management.py, learning/children/test_child_runner.py, learning/children/test_expression_language.py."""
from datetime import datetime, timedelta

from learning.children.unit_context import UnitContext
from model.timeline.timeline_builder import incident_timeline
from tests.support.profile_fixtures import make_incident


START = datetime(2026, 8, 3, 9)

AS_OF = datetime(2026, 8, 20)

def quick_close(incident_id, minutes, close_notes=True, work_notes=False):
    """L1 továbbítja a G csoportnak, ott `minutes` perc után lezárják."""
    t = lambda m: (START + timedelta(minutes=m)).isoformat()
    events = [
        (t(0), "state", None, "1"),
        (t(0), "assignment_group", "", "L1"),
        (t(5), "assignment_group", "L1", "G"),
        (t(6), "assigned_to", "", "resolver"),
    ]
    if work_notes:
        events.append((t(7), "work_notes", "", "megnéztem"))
    if close_notes:
        events.append((t(5 + minutes), "close_notes", "", "kész"))
    events.append((t(5 + minutes), "state", "1", "6"))
    return incident_timeline(make_incident(incident_id, events), AS_OF)

IMMEDIATE = 'szakasz_perc < 15 and lezarta and kitoltve("close_notes")'

def stint_case(steps):
    """L1 -> G; G-nél a 10. percben névre veszik, utána `steps` (perc, mező, régi, új)."""
    t = lambda m: (START + timedelta(minutes=m)).isoformat()
    events = [(t(0), "state", None, "1"), (t(0), "assignment_group", "", "L1"), (t(5), "assignment_group", "L1", "G"),
              (t(10), "assigned_to", "", "resolver"), (t(11), "email", "x", "")]   # az email rendszerzaj
    events += [(t(m), f, o, n) for m, f, o, n in steps]
    timeline = incident_timeline(make_incident("I1", events), AS_OF)
    return UnitContext(timeline, timeline.stints[1], {})

def signal(affected, group="G"):
    return {"diagnostic": "FEATURE_USAGE", "subject": f"work_notes|{group}",
            "items": (("feature", "work_notes"), ("assignment_group", group)),
            "details": {"feature": "work_notes", "group": group}, "severity": "EXCEEDED_EXTREME",
            "description": "sosem ír munkajegyzetet", "affected": affected}
