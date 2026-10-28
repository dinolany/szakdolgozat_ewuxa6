from abc import abstractmethod
from typing import Any, Dict, Iterable, Mapping, Tuple

from model.diagnostics.core.base_diagnosis import BaseDiagnosis
from model.diagnostics.core.measurement_context import MeasurementContext
from model.diagnostics.core.observation import Observation
from model.diagnostics.core.window_type import WindowType
from model.diagnostics.history_refs import make_history_ref
from model.history_line import HistoryLine
from model.incident import Incident


class SingleTicketDiagnosis(BaseDiagnosis):
    """Egyetlen incidens életútját vizsgáló diagnosztika; ablak nélkül, az incidens teljes életén."""

    family = "SINGLE_TICKET"
    affected_type = "incident"
    window_type = WindowType.NONE

    @abstractmethod
    def accumulate(self, incident: Incident, ctx: MeasurementContext) -> Iterable[Tuple[HistoryLine, Mapping[str, Any]]]:
        """Időrendben bejárja az incidens releváns eseményeit (a mérés pillanatáig); minden lépésnél
        az addig számolt details."""
        raise NotImplementedError

    def initial_details(self, incident: Incident, ctx: MeasurementContext) -> Mapping[str, Any]:
        """A details értéke, ha az incidensnek még nincs releváns eseménye."""
        return {}

    def _observe(self, ctx, window):
        for incident in ctx.incidents:
            yield self._observe_incident(incident, ctx)

    def _observe_subject(self, ctx, window, subject):
        for incident in ctx.incidents:
            if str(incident.id) == subject:
                yield self._observe_incident(incident, ctx)

    def _entity(self, incident: Incident, ctx: MeasurementContext) -> Dict[str, Any]:
        """A jegy entitásai a mérés pillanatában: az útjába eső csoportok, a jelenlegi csoport, a service."""
        from model.timeline.timeline_builder import incident_timeline
        timeline = incident_timeline(incident, ctx.measured_at)
        if timeline is None:
            return {}
        groups = list(timeline.groups)
        return {"assignment_group": groups, "current_group": timeline.value_at("assignment_group"),
                "business_service": timeline.value_at("business_service"), "subcategory": timeline.value_at("subcategory"),
                "contact_type": timeline.value_at("contact_type"), "route": " > ".join(groups)}

    def signal_items(self, subject: str, details: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
        """A jegy és a leírása: az útjába eső csoportok, az útvonal, a service, az alkategória, a bejelentés módja.
        A tanuló körben ezekre épülhet szabály, csomópont és szűkebb küszöb (az incidens maga egyedi, abból nem)."""
        entity = details.get("entity") or {}
        items = [("incident", subject)] + [("assignment_group", g) for g in entity.get("assignment_group") or []]
        for dimension in ("route", "business_service", "subcategory", "contact_type"):
            if entity.get(dimension):
                items.append((dimension, str(entity[dimension])))
        return tuple(dict.fromkeys(items))

    def _observe_incident(self, incident: Incident, ctx: MeasurementContext) -> Observation:
        incident_key = str(incident.id)
        entity = self._entity(incident, ctx)
        details: Mapping[str, Any] = dict(self.initial_details(incident, ctx))
        if entity:
            details["entity"] = entity
        refs = []
        trigger_ref = None
        for event, step_details in self.accumulate(incident, ctx):
            details = dict(step_details)
            if entity:
                details["entity"] = entity
            ref = make_history_ref(incident_key, event, self.key.lower())
            refs.append(ref)
            # A legutóbbi átlépés eseménye: küszöb alá esésnél törlődik, újabb átlépésnél újraíródik
            thresholds, _ = self.thresholds_for(ctx, incident_key, details)
            if self.classify(details, thresholds) is None:
                trigger_ref = None
            elif trigger_ref is None:
                trigger_ref = ref
        return Observation(
            subject=incident_key,
            details=details,
            affected=(incident_key,),
            history_refs=tuple(refs),
            trigger_ref=trigger_ref,
        )
