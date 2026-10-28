"""Ping-pong: a ticket csoportok közti pattogása.

Ugyanazt számolja, mint a feltárás `reassignments` mérése (TicketTimeline.reassignments): a
csoportváltásokat a mérés pillanatáig, az L1-ből való továbbítás nélkül, mert az a normál út
(ha a tudástár megnevezi az L1-et). Az L1-be való visszakerülés számít, és külön is jelezzük,
mert az a félreirányítás jele.
"""

from typing import Any, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core import MeasurementContext, ParameterDef, SingleTicketDiagnosis
from model.history_line import HistoryLine
from model.knowledge.predicates import L1_LEVEL
from model.severity_level import SeverityLevel
from model.timeline.timeline_builder import incident_timeline


class PingPongDiagnosis(SingleTicketDiagnosis):
    key = "PING_PONG"
    config_key = "ping_pong"
    issue_category = "PING_PONG"
    metric_key = "moves"
    metric_label = "csoportváltás"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("moderate_moves", "Mérsékelt csoportváltás", 3, 1, 100, 1,
                         "Ennyi csoportváltástól aktív (az L1-ből való továbbítás nem számít).", "db"),
            ParameterDef("extreme_moves", "Extrém csoportváltás", 5, 1, 100, 1, "Extrém súlyossági szint küszöbe.", "db"),
        ]

    signal_label = "Ping-pong"

    def issue_id_for(self, occurrence: str) -> str:
        return f"ISSUE-{self.key}-{occurrence}"  # a régi formátum: a tárolt issue-k megmaradnak

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        moves = details.get("moves", 0)
        if moves >= thresholds["extreme_moves"]:
            return SeverityLevel.EXCEEDED_EXTREME
        if moves >= thresholds["moderate_moves"]:
            return SeverityLevel.EXCEEDED_MODERATE
        return None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        returned = f", ebből {details['returned_to_l1']}× vissza az L1-be" if details.get("returned_to_l1") else ""
        return f"Ping-pong: {details.get('moves', 0)} csoportváltás{returned}, {len(details.get('groups', []))} csoport."

    def initial_details(self, incident, ctx: MeasurementContext) -> Mapping[str, Any]:
        return {"moves": 0, "returned_to_l1": 0, "groups": [], "l1_known": bool(ctx.knowledge.levels)}

    def accumulate(self, incident, ctx: MeasurementContext) -> Iterable[Tuple[HistoryLine, Mapping[str, Any]]]:
        timeline = incident_timeline(incident, ctx.measured_at)
        if timeline is None:
            return
        levels = ctx.knowledge.levels
        groups = timeline.groups
        returned = 0
        for count, move in enumerate(timeline.reassignments(levels), 1):
            returned += levels.get(move.new) == L1_LEVEL
            yield move.event, {"moves": count, "returned_to_l1": returned, "groups": groups, "l1_known": bool(levels)}
