"""Közös tesztadatok és -segédek.

Használja: engine/test_issue_manager.py, learning/analysis/test_candidate_analysis.py, learning/children/test_child_management.py, learning/feedback/test_generalize.py, learning/feedback/test_review.py, learning/mining/test_baseline.py, learning/mining/test_contrast_mining.py, learning/mining/test_profiles.py, learning/mining/test_trend.py, model/diagnostics/test_backlog_aging.py, model/diagnostics/test_feature_usage.py, model/diagnostics/test_flapping.py, model/diagnostics/test_issue_trend.py, model/diagnostics/test_ping_pong.py, model/diagnostics/test_similar_description.py, model/diagnostics/test_ticket_volume.py, model/diagnostics/test_unassigned.py, model/test_ticket_timeline.py, model/test_working_time.py."""
from datetime import datetime, timedelta
import random

from learning import MEASURES_BY_KEY, TicketProfile
from model.history_line import HistoryLine
from model.incident import Incident


PICKUP = MEASURES_BY_KEY["pickup_delay"]

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def make_incident(incident_id, events):
    incident = Incident(incident_id)
    for timestamp, field, old, new in events:
        incident.apply_event(HistoryLine({
            "set": incident_id, "timestamp": timestamp, "field": field, "old_value": old, "new_value": new,
        }))
    return incident

def make_profile(index, opened_at, group, service, bad, extra=None):
    dimensions = {
        "assignment_group": group,
        "business_service": service,
        "date": opened_at.date().isoformat(),
        "weekday": WEEKDAYS[opened_at.weekday()],
        "hour": f"{opened_at.hour:02d}",
    }
    dimensions.update(extra or {})
    return TicketProfile(f"INC{index:05d}", opened_at, dimensions, {"pickup_delay": 20.0 if bad else 1.0})

def synthetic_profiles(seed, planted, count=700, base_bad=0.04):
    """Két hét szintetikus ticket; `planted` esetén a G3 csoport vasárnapi ticketjei mind rosszak."""
    rng = random.Random(seed)
    start = datetime(2026, 8, 3)  # hétfő
    profiles = []
    for index in range(count):
        opened = start + timedelta(days=rng.randrange(14), hours=rng.randrange(8, 18))
        group = f"G{rng.randrange(1, 9)}"
        service = f"S{rng.randrange(1, 7)}"
        is_planted = planted and group == "G3" and opened.weekday() == 6
        profiles.append(make_profile(index, opened, group, service, is_planted or rng.random() < base_bad))
    return profiles
