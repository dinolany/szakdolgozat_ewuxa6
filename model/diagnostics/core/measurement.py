from dataclasses import dataclass
from datetime import datetime
import hashlib
from typing import Any, Dict, Mapping, Optional, Tuple

from model.diagnostics.core.measurement_window import MeasurementWindow
from model.severity_level import SeverityLevel


def make_issue_id(diagnostic_key: str, occurrence_key: str) -> str:
    """Az egyetlen issue-azonosító képző: diagnosztika + előfordulás."""
    digest = hashlib.sha1(occurrence_key.encode("utf-8")).hexdigest()[:12]
    return f"ISSUE-{diagnostic_key}-{digest}"

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
        """Túllépte-e a küszöböt. A `severity is None` kizárólag ezt jelenti („mérve, küszöb alatt”): nem mérhető
        subjectről mérés nem készül (ld. Observation)."""
        return self.severity is not None
