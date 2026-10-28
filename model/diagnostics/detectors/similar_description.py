"""Hasonló leírású ticketek halmozódása: ugyanaz a (normalizált) rövid leírás több, az ablakban
nyitott ticketen (lehetséges közös ok, pl. nagyobb kiesés).

A leírás a history-ból jön: a ticket első kitöltött rövid leírása (amit a bejelentéskor rögzítettek),
nem az incidens éppen beírt mezője.
"""

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.common import normalize_text
from model.diagnostics.core import CrossTicketDiagnosis, LOOKBACK_KEY, MeasurementContext, MeasurementWindow, Observation, ParameterDef, WindowType
from model.diagnostics.history_refs import make_history_ref
from model.severity_level import SeverityLevel
from model.timeline.field_change import FieldChange
from model.timeline.timeline_builder import incident_timeline


class SimilarDescriptionDiagnosis(CrossTicketDiagnosis):
    key = "SIMILAR_DESCRIPTION"
    config_key = "similar_description"
    issue_category = "CROSS_TICKET"
    window_type = WindowType.SLIDING
    metric_key = "count"
    metric_label = "hasonló ticket"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("moderate_count", "Mérsékelt darabszám", 3, 1, 10000, 1, "Ennyi azonos leírású ticket aktív issue.", "db"),
            ParameterDef("extreme_count", "Extrém darabszám", 8, 1, 10000, 1, "Extrém súlyossági szint küszöbe.", "db"),
        ]

    @classmethod
    def window_definitions(cls) -> List[ParameterDef]:
        return [ParameterDef(LOOKBACK_KEY, "Visszatekintés", 1440, 60, 43200, 60,
                             "Az ennyi percen belül nyitott ticketek leírásait veti össze (1440 perc = 1 nap).", "perc")]

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        count = details.get("count", 0)
        if count >= thresholds["extreme_count"]:
            return SeverityLevel.EXCEEDED_EXTREME
        if count >= thresholds["moderate_count"]:
            return SeverityLevel.EXCEEDED_MODERATE
        return None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        return f"{details.get('count', 0)} ticket nyílt azonos rövid leírással: „{details.get('description', subject)}”."

    def observe_incidents(self, incidents, window, ctx):  # a _observe felülírása miatt nem használt
        return ()

    def _observe(self, ctx: MeasurementContext, window: MeasurementWindow) -> Iterable[Observation]:
        groups: Dict[str, List[Tuple[str, FieldChange]]] = defaultdict(list)
        for incident in ctx.incidents:
            timeline = incident_timeline(incident, ctx.measured_at)
            if timeline is None or not window.contains(timeline.opened_at):
                continue
            first = next((c for c in timeline.changes_of("short_description") if c.new), None)
            text = normalize_text(first.new) if first else ""
            if text and text != "n/a":
                groups[text].append((timeline.incident_id, first))
        return [
            Observation(
                subject=text,
                details={"description": members[0][1].new, "normalized": text, "count": len(members)},
                affected=tuple(sorted(incident_id for incident_id, _ in members)),
                history_refs=tuple(make_history_ref(i, change.event, "similar_short_description") for i, change in members),
            )
            for text, members in sorted(groups.items())
        ]
