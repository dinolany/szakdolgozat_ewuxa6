import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from model.diagnostics.core.window import MeasurementWindow, WindowType
from model.history_line import HistoryLine
from model.incident import Incident
from model.issue import Issue, SeverityLevel
from model.knowledge.knowledge_base import KnowledgeView


class Trigger:
    """A mérést indító események."""

    EVENT_BATCH = "EVENT_BATCH"              # feldolgozott eseményköteg után
    WINDOW_CLOSED = "WINDOW_CLOSED"          # lezárult egy TUMBLING vödör
    MANUAL = "MANUAL"                        # kézi indítás
    FULL_REANALYSIS = "FULL_REANALYSIS"      # teljes újraelemzés
    THRESHOLD_CHANGED = "THRESHOLD_CHANGED"  # küszöbparaméter változott
    WINDOW_CHANGED = "WINDOW_CHANGED"        # ablakparaméter változott


def make_issue_id(diagnostic_key: str, occurrence_key: str) -> str:
    """Az egyetlen issue-azonosító képző: diagnosztika + előfordulás."""
    digest = hashlib.sha1(occurrence_key.encode("utf-8")).hexdigest()[:12]
    return f"ISSUE-{diagnostic_key}-{digest}"


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


@dataclass(frozen=True)
class Observation:
    """Amit a konkrét diagnosztika egy subjectről megállapít, küszöbtől függetlenül."""

    subject: str
    details: Mapping[str, Any]
    affected: Tuple[str, ...] = ()
    history_refs: Tuple[Dict[str, Any], ...] = ()
    trigger_ref: Optional[Dict[str, Any]] = None  # csak incidensalapú mérésnél: a küszöbátlépés eseménye


@dataclass(frozen=True)
class Measurement:
    """Egy diagnosztika egy subjectre vonatkozó, besorolt mérési eredménye. Ebből lesz (vagy frissül) az Issue."""

    # Identitás
    diagnostic_key: str
    issue_id: str
    occurrence_key: str
    subject: str
    # Besorolás
    family: str
    issue_category: str
    affected_type: str
    # Eredmény
    severity: Optional[SeverityLevel]
    metric_value: float
    metric_label: str
    description: str
    details: Mapping[str, Any]
    # Bizonyíték
    affected_objects: Tuple[str, ...]
    history_refs: Tuple[Dict[str, Any], ...]
    # Mérés
    trigger: str
    measured_at: Optional[datetime]
    window: MeasurementWindow
    params_snapshot: Mapping[str, Any]
    trigger_ref: Optional[Dict[str, Any]] = None

    @property
    def exceeded(self) -> bool:
        return self.severity is not None


@dataclass(frozen=True)
class DiagnosticRun:
    """Egy diagnosztika egy futásának eredménye; az IssueManager ez alapján kezeli a saját issue-it."""

    diagnostic_key: str
    window_type: WindowType
    trigger: str
    measured_at: Optional[datetime]
    window: MeasurementWindow
    measurements: List[Measurement]
    # None: a diagnosztika összes issue-ja a futás hatókörébe tartozik; különben csak a felsoroltak
    scope: Optional[FrozenSet[str]] = None
