"""Közös tesztadatok és -segédek.

Használja: engine/test_issue_manager.py, learning/test_discovery_manager.py, model/diagnostics/test_backlog_aging.py, model/diagnostics/test_flapping.py, model/diagnostics/test_issue_trend.py, model/diagnostics/test_ping_pong.py, model/diagnostics/test_similar_description.py, model/diagnostics/test_suppression.py, model/diagnostics/test_unassigned.py."""
from datetime import datetime, timedelta

from model.diagnostics.core import MeasurementContext, Trigger
from model.knowledge.knowledge_view import KnowledgeView
from tests.support.profile_fixtures import make_incident


T0 = datetime(2026, 8, 3, 8)

L1 = KnowledgeView(levels={"L1": "L1"})

def at(hours):
    return (T0 + timedelta(hours=hours)).isoformat()

def measured(diagnostic, incidents, hours, knowledge=KnowledgeView()):
    run = diagnostic.run(MeasurementContext(Trigger.MANUAL, T0 + timedelta(hours=hours), incidents, [], knowledge=knowledge))
    return {m.subject: m for m in run.measurements if m.exceeded}

BOUNCING = make_incident("INC1", [
    (at(0), "assignment_group", "", "L1"),
    (at(1), "assignment_group", "L1", "A"),
    (at(2), "assignment_group", "A", "B"),
    (at(3), "assignment_group", "B", "L1"),   # vissza az L1-be
    (at(4), "assignment_group", "L1", "C"),
    (at(9), "assignment_group", "C", "D"),
])
