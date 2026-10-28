from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from model.history_line import HistoryLine
from model.incident import Incident
from model.issue import Issue
from model.knowledge.knowledge_view import KnowledgeView


@dataclass(frozen=True)
class MeasurementContext:
    """Egy diagnosztika futásának bemenete (a küszöbök és az ablakparaméterek már a sajátjai)."""

    trigger: str
    measured_at: datetime
    incidents: Sequence[Incident] = ()
    issues: Sequence[Issue] = ()
    thresholds: Mapping[str, Any] = field(default_factory=dict)
    window_params: Mapping[str, Any] = field(default_factory=dict)
    # EVENT_BATCH triggernél a köteg által érintett események
    batch_events: Sequence[HistoryLine] = ()
    # A mérés pillanatában érvényes tények a közös tudástárból (pl. L1-csoportok, funkció-elérhetőség)
    knowledge: KnowledgeView = field(default_factory=KnowledgeView)
    # Jóváhagyásos mód: a diagnosztika nem nyit új issue-t (a jelzései a tanuló körbe mennek),
    # csak a már elfogadott feladatait (meglévő issue-it) méri újra
    review: bool = False

    def for_diagnostic(self, thresholds: Mapping[str, Any], window_params: Mapping[str, Any]) -> "MeasurementContext":
        return replace(self, thresholds=dict(thresholds), window_params=dict(window_params))
