from abc import abstractmethod
from typing import ClassVar, FrozenSet, Iterable, List, Optional

from model.diagnostics.core.base_diagnosis import BaseDiagnosis
from model.diagnostics.core.measurement_context import MeasurementContext
from model.diagnostics.core.measurement_window import MeasurementWindow, parse_timestamp
from model.diagnostics.core.observation import Observation
from model.diagnostics.core.window_type import WindowType
from model.history_line import HistoryLine
from model.incident import Incident


class CrossTicketDiagnosis(BaseDiagnosis):
    """Több incidens közös mintázatát vizsgáló diagnosztika, a saját mérési ablakán belül.

    Egy incidens akkor tartozik az ablakba, ha volt az ablakban releváns aktivitása
    (activity_fields) ÉS megfelel a diagnosztika belépési feltételének (is_eligible).
    SNAPSHOT ablaknál csak a belépési feltétel számít.
    """

    family = "CROSS_TICKET"
    affected_type = "incident"
    window_type = WindowType.SLIDING
    activity_fields: ClassVar[Optional[FrozenSet[str]]] = None  # None: bármilyen esemény aktivitás

    @abstractmethod
    def observe_incidents(
        self, incidents: List[Incident], window: MeasurementWindow, ctx: MeasurementContext
    ) -> Iterable[Observation]:
        raise NotImplementedError

    def is_eligible(self, incident: Incident, ctx: MeasurementContext) -> bool:
        return True

    def is_activity(self, event: HistoryLine) -> bool:
        return self.activity_fields is None or event.field.strip().lower() in self.activity_fields

    def activity_events(self, incident: Incident, window: MeasurementWindow) -> List[HistoryLine]:
        return [
            event for event in incident.history
            if self.is_activity(event) and window.contains(parse_timestamp(event.timestamp))
        ]

    def incidents_in_window(self, ctx: MeasurementContext, window: MeasurementWindow) -> List[Incident]:
        needs_activity = window.type.is_timed
        return [
            incident for incident in ctx.incidents
            if self.is_eligible(incident, ctx)
            and (not needs_activity or self.activity_events(incident, window))
        ]

    def _observe(self, ctx, window):
        return self.observe_incidents(self.incidents_in_window(ctx, window), window, ctx)
