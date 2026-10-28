from abc import ABC, abstractmethod
from typing import Any, ClassVar, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core.suppression import suppressing_rule
from model.diagnostics.core.threshold_overrides import resolve as resolve_thresholds, source_summary

from model.diagnostics.core.measurement import (
    DiagnosticRun,
    Measurement,
    MeasurementContext,
    Observation,
    Trigger,
    make_issue_id,
)
from model.diagnostics.core.params import ParameterDef, resolve_params
from model.diagnostics.core.window import MeasurementWindow, WindowType, parse_timestamp
from model.diagnostics.history_refs import make_history_ref
from model.history_line import HistoryLine
from model.incident import Incident
from model.issue import Issue, SeverityLevel


class BaseDiagnosis(ABC):
    """Minden diagnosztika közös szerződése.

    A konkrét diagnosztika csak mér (observe) és besorol (classify). Az azonosítóképzést,
    az ablak feloldását és a Measurement felépítését a base végzi; az issue-k életciklusát
    az IssueManager kezeli a DiagnosticRun alapján.
    """

    # --- a konkrét diagnosztika adja meg ---
    key: ClassVar[str] = ""
    issue_category: ClassVar[str] = ""           # csak megjelenítés; default: key
    triggers: ClassVar[FrozenSet[str]] = frozenset({Trigger.EVENT_BATCH, Trigger.MANUAL, Trigger.FULL_REANALYSIS})
    metric_key: ClassVar[str] = ""               # a details kulcsa, amelyből a metric_value lesz
    metric_label: ClassVar[str] = ""
    # Alapból jóváhagyásos-e: a jelzései a tanuló körbe mennek, issue (feladat) csak elfogadásból lesz
    review_by_default: ClassVar[bool] = False
    signal_label: ClassVar[str] = ""             # a jelzés-jelöltek megnevezése a felületen

    # --- a család adja meg ---
    family: ClassVar[str] = ""
    affected_type: ClassVar[str] = "incident"
    window_type: ClassVar[WindowType] = WindowType.NONE
    phase: ClassVar[int] = 1                     # 1: ticket-diagnosztikák, 2: issue-diagnosztikák

    # --- a base tölti ki ---
    config_key: ClassVar[str] = ""               # a küszöb- és ablakkonfiguráció kulcsa; default: key.lower()
    _registered_keys: ClassVar[Dict[str, str]] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "key" not in cls.__dict__:
            return  # családosztály vagy absztrakt köztes osztály
        if not cls.key:
            raise TypeError(f"{cls.__name__}: a 'key' nem lehet üres.")
        qualified = f"{cls.__module__}.{cls.__qualname__}"
        owner = BaseDiagnosis._registered_keys.setdefault(cls.key, qualified)
        if owner != qualified:
            raise TypeError(f"{cls.__name__}: a '{cls.key}' kulcsot már a {owner} használja.")
        if not cls.__dict__.get("config_key"):
            cls.config_key = cls.key.lower()
        if not cls.__dict__.get("issue_category"):
            cls.issue_category = cls.key
        length_key = cls.window_type.length_key
        if length_key and length_key not in {d.key for d in cls.window_definitions()}:
            raise TypeError(f"{cls.__name__}: {cls.window_type} ablakhoz kötelező a '{length_key}' ablakparaméter.")

    # ------------------------------------------------------------------
    # Paraméterek
    # ------------------------------------------------------------------

    @classmethod
    @abstractmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        raise NotImplementedError

    @classmethod
    def window_definitions(cls) -> List[ParameterDef]:
        """Az ablak állítható paraméterei. NONE és SNAPSHOT ablaknál nincs ilyen."""
        return []

    def thresholds(self, ctx: MeasurementContext) -> Dict[str, Any]:
        return resolve_params(self.threshold_definitions(), ctx.thresholds)

    def window_params(self, ctx: MeasurementContext) -> Dict[str, Any]:
        return resolve_params(self.window_definitions(), ctx.window_params)

    def current_params(self, ctx: MeasurementContext) -> Dict[str, Dict[str, Any]]:
        return {"thresholds": self.thresholds(ctx), "window": self.window_params(ctx)}

    def entity_items(self, subject: str, details: Mapping[str, Any]) -> List[Tuple[str, str]]:
        """A mért eset entitásai (csoport, útvonal, service…), amelyekre szűkebb hatókörű küszöb illeszkedhet:
        a jelzés leírása, és amit a diagnosztika a details["entity"] alatt megad."""
        items = list(self.signal_items(subject, details))
        for dimension, value in (details.get("entity") or {}).items():
            for v in (value if isinstance(value, (list, tuple)) else [value]):
                if v:
                    items.append((dimension, str(v)))
        return list(dict.fromkeys(items))   # ismétlés nélkül, sorrendtartóan

    def thresholds_for(self, ctx: MeasurementContext, subject: str, details: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """A mért esetre érvényes küszöbök: kézi > tanult > globális (models.diagnostics.core.threshold_overrides)."""
        base = self.thresholds(ctx)
        overrides = getattr(ctx.knowledge, "threshold_overrides", ())
        if not overrides:
            return base, {key: "alapértelmezett" for key in base}
        return resolve_thresholds(base, overrides, self.key, self.entity_items(subject, details))

    # ------------------------------------------------------------------
    # A konkrét diagnosztika feladata
    # ------------------------------------------------------------------

    @abstractmethod
    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        """Besorolás kizárólag a details alapján; None, ha nincs küszöbtúllépés."""
        raise NotImplementedError

    @abstractmethod
    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        raise NotImplementedError

    def metric_value(self, details: Mapping[str, Any]) -> float:
        return details.get(self.metric_key, 0) if self.metric_key else 0

    # ------------------------------------------------------------------
    # A család feladata
    # ------------------------------------------------------------------

    @abstractmethod
    def _observe(self, ctx: MeasurementContext, window: MeasurementWindow) -> Iterable[Observation]:
        raise NotImplementedError

    def _observe_subject(self, ctx: MeasurementContext, window: MeasurementWindow, subject: str) -> Iterable[Observation]:
        return (observation for observation in self._observe(ctx, window) if observation.subject == subject)

    def signals(self, ctx: MeasurementContext) -> List[Tuple[Observation, SeverityLevel]]:
        """A jóváhagyásra váró jelzések: a túllépő megfigyelések, amelyekhez még nincs issue (feladat)."""
        window = self.resolve_window(ctx)
        known = {issue.subject for issue in ctx.issues if issue.diagnostic_key == self.key and not issue.is_deleted}
        result = []
        for observation in self._observe(ctx, window):
            thresholds, _ = self.thresholds_for(ctx, observation.subject, observation.details)
            severity = self.classify(observation.details, thresholds)
            if severity is not None and observation.subject not in known:
                result.append((observation, severity))
        return result

    def signal_items(self, subject: str, details: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
        """A jelzés leírása dimenzió-érték párokként (a tanuló kör szabályaihoz és csomópontjaihoz)."""
        return (("subject", subject),)

    def child_units(self, ctx: MeasurementContext) -> Dict[str, Tuple[List[Tuple[str, Optional[int]]], List[Tuple[str, Optional[int]]]]]:
        """Almérésekhez: jelzésenként (subject) az érintett esetek és az összehasonlító esetek
        ((incidens, szakasz sorszáma vagy None) párok). Alapból nincs: az almérés nem fut a jelzésen."""
        return {}

    def accept_signal(self, ctx: MeasurementContext, subject: str) -> DiagnosticRun:
        """Elfogadott jelzés -> feladat: a subject mérése új issue-ként (jóváhagyásos módban is)."""
        window = self.resolve_window(ctx)
        measurements = [self._to_measurement(o, ctx, window) for o in self._observe_subject(ctx, window, subject)][:1]
        return DiagnosticRun(self.key, self.window_type, ctx.trigger, ctx.measured_at, window, measurements,
                             frozenset(m.issue_id for m in measurements))

    def issue_id_for(self, occurrence: str) -> str:
        """Az issue azonosítója egy előforduláshoz (régi formátumú diagnosztika felülírhatja,
        hogy a tárolt issue-i ne árvuljanak el)."""
        return make_issue_id(self.key, occurrence)

    def occurrence_key(self, subject: str, window: MeasurementWindow) -> str:
        """TUMBLING ablaknál minden vödör külön előfordulás; egyébként a subject maga."""
        if self.window_type is WindowType.TUMBLING and window.start is not None:
            return f"{subject}|{window.start.isoformat()}"
        return subject

    # ------------------------------------------------------------------
    # Futtatás (nem felülírandó)
    # ------------------------------------------------------------------

    def resolve_window(self, ctx: MeasurementContext) -> MeasurementWindow:
        params = self.window_params(ctx)
        if self.window_type is WindowType.SNAPSHOT:
            return MeasurementWindow.snapshot(ctx.measured_at)
        if self.window_type is WindowType.SLIDING:
            return MeasurementWindow.sliding(ctx.measured_at, params[self.window_type.length_key])
        if self.window_type is WindowType.TUMBLING:
            return MeasurementWindow.tumbling(ctx.measured_at, params[self.window_type.length_key])
        return MeasurementWindow.none()

    def run(self, ctx: MeasurementContext) -> DiagnosticRun:
        """Teljes mérés: minden túllépés, plusz a már issue-val rendelkező subjectek küszöb alatti mérése."""
        window = self.resolve_window(ctx)
        known = {
            issue.issue_id for issue in ctx.issues
            if issue.diagnostic_key == self.key and not issue.is_deleted
        }
        measurements = []
        suppressions = getattr(ctx.knowledge, "suppressions", ())
        for observation in self._observe(ctx, window):
            measurement = self._to_measurement(observation, ctx, window)
            # jóváhagyásos módban új issue nem nyílik; a meglévő feladatok mérése folytatódik
            if measurement.issue_id in known:
                measurements.append(measurement)
            elif measurement.exceeded and not ctx.review:
                # tanult elnyomás: ha a leírására illik egy erre a detektorra szóló elvető szabály, nem nyílik issue
                if not (suppressions and suppressing_rule(self.key, self.entity_items(observation.subject, observation.details),
                                                          suppressions)):
                    measurements.append(measurement)
        return DiagnosticRun(self.key, self.window_type, ctx.trigger, ctx.measured_at, window, measurements)

    def remeasure(self, issue: Issue, ctx: MeasurementContext) -> DiagnosticRun:
        """Egyetlen issue újramérése a jelenlegi paraméterekkel.

        Időablakos mérésnél az ablak az issue saját ablakvégéhez horgonyzott, és az új
        hosszra rövidül vagy tolódik ki; egyébként a jelenlegi mérési pillanat ablaka.
        """
        if not issue.subject:
            # Régi, subject nélküli issue: nem mérhető újra, ezért hozzá sem nyúlunk
            return DiagnosticRun(self.key, self.window_type, ctx.trigger, ctx.measured_at, MeasurementWindow.none(), [], frozenset())
        window = self._remeasure_window(issue, ctx)
        measurements = [
            self._to_measurement(observation, ctx, window, issue.issue_id, issue.occurrence_key)
            for observation in self._observe_subject(ctx, window, issue.subject)
        ][:1]
        return DiagnosticRun(
            self.key, self.window_type, ctx.trigger, ctx.measured_at, window, measurements, frozenset({issue.issue_id})
        )

    def _remeasure_window(self, issue: Issue, ctx: MeasurementContext) -> MeasurementWindow:
        stored = MeasurementWindow.from_values(issue.window_type, issue.window_start, issue.window_end)
        if not self.window_type.is_timed or stored.type is not self.window_type or stored.end is None:
            return self.resolve_window(ctx)
        return stored.resized(self.window_params(ctx)[self.window_type.length_key])

    def _to_measurement(
        self,
        observation: Observation,
        ctx: MeasurementContext,
        window: MeasurementWindow,
        issue_id: Optional[str] = None,
        occurrence_key: Optional[str] = None,
    ) -> Measurement:
        params = self.current_params(ctx)
        thresholds, sources = self.thresholds_for(ctx, observation.subject, observation.details)
        details = dict(observation.details)
        if any(s != "alapértelmezett" for s in sources.values()):
            # a mérés nem a globális küszöbbel dőlt el: látszódjon, melyikkel és honnan
            details["threshold_source"] = source_summary(sources)
            details["thresholds_used"] = {k: v for k, v in thresholds.items() if sources.get(k) != "alapértelmezett"}
        occurrence = occurrence_key or self.occurrence_key(observation.subject, window)
        return Measurement(
            diagnostic_key=self.key,
            issue_id=issue_id or self.issue_id_for(occurrence),
            occurrence_key=occurrence,
            subject=observation.subject,
            family=self.family,
            issue_category=self.issue_category,
            affected_type=self.affected_type,
            severity=self.classify(observation.details, thresholds),
            metric_value=self.metric_value(observation.details),
            metric_label=self.metric_label,
            description=self.describe(observation.subject, observation.details),
            details=details,
            affected_objects=tuple(observation.affected),
            history_refs=tuple(observation.history_refs),
            trigger=ctx.trigger,
            measured_at=ctx.measured_at,
            window=window,
            params_snapshot=params,
            trigger_ref=observation.trigger_ref,
        )


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
        from model.timeline.ticket_timeline import incident_timeline   # késleltetett: a timeline a diagnosztikai magra épül
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
