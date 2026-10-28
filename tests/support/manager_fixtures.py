"""Közös tesztadatok és -segédek.

Használja: learning/children/test_child_management.py, learning/feedback/test_learned_thresholds.py, learning/mining/test_contrast_mining.py, learning/test_discovery_manager.py, learning/test_discovery_service.py, model/diagnostics/test_suppression.py."""
from datetime import datetime, timedelta

from learning.mining.contrast_mining import PatternResult
from learning.state.candidate import make_candidate_id


DAY = datetime(2026, 8, 10)  # hétfő

def result(items, bad_ids, measure="pickup_delay", lift=5.0, q=0.01, alternatives=()):
    items = tuple(sorted(items))
    return PatternResult(
        measure=measure, items=items, bad_count=len(bad_ids), group_size=len(bad_ids) * 2, population=500,
        base_rate=0.05, rate=0.5, lift=lift, expected=1.0, excess=len(bad_ids) - 1.0, p_value=q / 10, q_value=q,
        distinct_days=1, bad_incident_ids=tuple(bad_ids), alternatives=tuple(tuple(sorted(a)) for a in alternatives),
    )

def ingest(manager, results, day, counts=None):
    return manager.ingest(results, day, day - timedelta(days=7), day, counts)

def counts(bad, size, population_bad, population):
    return {"bad": bad, "size": size, "population_bad": population_bad, "population": population}

def cid(items, measure="pickup_delay"):
    from learning.mining.contrast_mining import pattern_key
    return make_candidate_id(measure, pattern_key(items))

G_SUN = (("assignment_group", "G"), ("weekday", "Sun"))

G_SUN_10 = (("assignment_group", "G"), ("hour", "10"), ("weekday", "Sun"))
