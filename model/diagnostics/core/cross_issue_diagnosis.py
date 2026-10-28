from abc import abstractmethod
from typing import Iterable, List

from model.diagnostics.core.base_diagnosis import BaseDiagnosis
from model.diagnostics.core.measurement_context import MeasurementContext
from model.diagnostics.core.measurement_window import MeasurementWindow
from model.diagnostics.core.observation import Observation
from model.diagnostics.core.window_type import WindowType
from model.issue import Issue


class CrossIssueDiagnosis(BaseDiagnosis):
    """A többi diagnosztika issue-in dolgozó diagnosztika; a ticket-diagnosztikák után fut."""

    family = "CROSS_ISSUE"
    affected_type = "issue"
    window_type = WindowType.SNAPSHOT
    phase = 2

    @abstractmethod
    def observe_issues(
        self, issues: List[Issue], window: MeasurementWindow, ctx: MeasurementContext
    ) -> Iterable[Observation]:
        raise NotImplementedError

    def _observe(self, ctx, window):
        issues = [
            issue for issue in ctx.issues
            if issue.diagnostic_key != self.key and not issue.is_deleted
        ]
        return self.observe_issues(issues, window, ctx)
