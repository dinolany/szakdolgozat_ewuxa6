"""Csoport nélküli nyitott ticket: a mérés pillanatában senkihez nem tartozik.

A history-ból, a mérés pillanatáig nézve (nem az incidens éppen beírt mezőiből): a ticket nyitott,
és nincs kitöltött megoldócsoportja. Az idő onnan számít, amióta így van (a nyitástól, vagy
attól, hogy a csoportot törölték).
"""

from typing import Any, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core import MeasurementContext, ParameterDef, SingleTicketDiagnosis
from model.history_line import HistoryLine
from model.severity_level import SeverityLevel
from model.timeline.fields import hours_between
from model.timeline.timeline_builder import incident_timeline


class UnassignedDiagnosis(SingleTicketDiagnosis):
    key = "UNASSIGNED_TICKET"
    config_key = "unassigned"
    issue_category = "UNASSIGNED_TICKET"
    metric_key = "hours"
    metric_label = "csoport nélkül (óra)"
    signal_label = "Csoport nélküli jegy"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("moderate_minutes", "Mérsékelt várakozás", 30, 0, 10080, 5,
                         "Ennyi perce csoport nélküli nyitott ticket aktív issue.", "perc"),
            ParameterDef("extreme_hours", "Extrém várakozás", 4, 1, 720, 1, "Extrém súlyossági szint küszöbe.", "óra"),
        ]

    def issue_id_for(self, occurrence: str) -> str:
        return f"ISSUE-{self.key}-{occurrence}:{self.key}"  # a régi formátum: a tárolt issue-k megmaradnak

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        if not details.get("unassigned"):
            return None
        hours = details.get("hours", 0.0)
        if hours >= thresholds["extreme_hours"]:
            return SeverityLevel.EXCEEDED_EXTREME
        if hours * 60 >= thresholds["moderate_minutes"]:
            return SeverityLevel.EXCEEDED_MODERATE
        return None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        return f"{subject} {details.get('hours', 0):.1f} órája nincs csoporthoz rendelve."

    def accumulate(self, incident, ctx: MeasurementContext) -> Iterable[Tuple[HistoryLine, Mapping[str, Any]]]:
        timeline = incident_timeline(incident, ctx.measured_at)
        if timeline is None or timeline.is_closed or timeline.value_at("assignment_group"):
            return
        cleared = [c for c in timeline.changes_of("assignment_group") if not c.new]
        anchor = cleared[-1] if cleared else timeline.changes[0]
        yield anchor.event, {"unassigned": True, "hours": hours_between(anchor.at, ctx.measured_at)}
