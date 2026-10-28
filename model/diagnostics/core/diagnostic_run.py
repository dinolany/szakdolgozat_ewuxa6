from dataclasses import dataclass
from datetime import datetime
from typing import FrozenSet, List, Optional

from model.diagnostics.core.measurement import Measurement
from model.diagnostics.core.measurement_window import MeasurementWindow
from model.diagnostics.core.window_type import WindowType


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
