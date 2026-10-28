"""Visszanyitott ticket: lezárt állapotból újra nyitottba került (a mérés pillanatáig, a history-ból)."""

from typing import Any, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core import MeasurementContext, ParameterDef, SingleTicketDiagnosis
from model.history_line import HistoryLine
from model.issue import SeverityLevel
from model.timeline.ticket_timeline import CLOSED_STATES, incident_timeline


class FlappingDiagnosis(SingleTicketDiagnosis):
    key = "FLAPPING_INCIDENT"
    config_key = "flapping"
    issue_category = "INCIDENT"
    metric_key = "transition_count"
    metric_label = "visszanyitás"
    signal_label = "Visszanyitott jegy"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("moderate_transitions", "Mérsékelt visszanyitások", 2, 1, 100, 1,
                         "Ennyi visszanyitástól aktív.", "db"),
            ParameterDef("extreme_transitions", "Extrém visszanyitások", 4, 1, 100, 1, "Extrém súlyossági szint küszöbe.", "db"),
        ]

    def issue_id_for(self, occurrence: str) -> str:
        return f"ISSUE-{self.key}-{occurrence}:{self.key}"  # a régi formátum: a tárolt issue-k megmaradnak

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        count = details.get("transition_count", 0)
        if count >= thresholds["extreme_transitions"]:
            return SeverityLevel.EXCEEDED_EXTREME
        if count >= thresholds["moderate_transitions"]:
            return SeverityLevel.EXCEEDED_MODERATE
        return None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        return f"{subject} jegyet {details.get('transition_count', 0)}× nyitották vissza a lezárása után."

    def accumulate(self, incident, ctx: MeasurementContext) -> Iterable[Tuple[HistoryLine, Mapping[str, Any]]]:
        timeline = incident_timeline(incident, ctx.measured_at)
        if timeline is None:
            return
        count, previous = 0, ""
        for change in timeline.changes_of("state"):
            # a state és az incident_state ugyanazt az átmenetet kétszer is rögzítheti
            if previous in CLOSED_STATES and change.new and change.new not in CLOSED_STATES:
                count += 1
                yield change.event, {"transition_count": count}
            if change.new:
                previous = change.new
