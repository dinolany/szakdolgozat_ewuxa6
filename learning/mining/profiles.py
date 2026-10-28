from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from learning.mining.measures import HOLD_TIME, PICKUP_DELAY, REASSIGNMENTS, SLA_ESCALATION, STINT_HOLD, STINT_PICKUP
from learning.mining.profile_set import ProfileSet
from learning.mining.ticket_profile import DimensionValue, TicketProfile
from model.diagnostics.history_refs import make_history_ref
from model.incident import Incident
from model.knowledge.predicates import L1_LEVEL
from model.timeline.field_change import FieldChange
from model.timeline.fields import HOLD_STATE, hours_between
from model.timeline.stint import Stint
from model.timeline.ticket_timeline import TicketTimeline
from model.timeline.timeline_builder import incident_timeline


# A mintakeresés dimenziói. Ticketen: a ticket élete alatt felvett összes érték (egy érték: szöveg,
# több: tuple, az első előfordulás sorrendjében); szakaszon: az érkezéskori érték, a csoport a szakaszé.
DIMENSIONS = (
    "business_service",
    "assignment_group",
    "u_responsibility_unit",
    "location",
    "subcategory",
    "impact",
    "contact_type",
)

TIME_DIMENSIONS = ("date", "weekday", "hour")

# Csak megjelenítésre (elemző felület): a kereső nem használja őket
# current_group: a jegyhez rendelt csoport (az incidens assignment_group értéke a mérés pillanatában)
DISPLAY_ONLY_DIMENSIONS = ("current_group", "time_basis")   # csak megjelenítés: a keresés nem bont szerintük

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

SLA_ESCALATED_VALUES = {"1", "true"}


def dimension_values(dimensions: Mapping[str, DimensionValue], dim: str) -> Tuple[str, ...]:
    """Egy mező értékei az adatlapon (a több értékű mezők, pl. a jegy útja, listaként).

    Hívja: candidate_analysis.analyze_candidate, candidate_analysis.breakdown, compare.window_summary, profiles.dimension_items,
    profiles.has_value, trend._counts."""
    value = dimensions.get(dim)
    if value is None or value == "":
        return ()
    return value if isinstance(value, tuple) else (value,)

def has_value(dimensions: Mapping[str, DimensionValue], dim: str, value: str) -> bool:
    """Illik-e a dimenzió az értékre (több értékű dimenziónál: szerepel-e köztük).

    Hívja: DiscoveryService._unit_sets, candidate_analysis.analyze_candidate, candidate_analysis.breakdown, baseline.pattern_counts,
    pattern_finding_diagnosis._matches."""
    return value in dimension_values(dimensions, dim)

def dimension_items(dimensions: Mapping[str, DimensionValue]) -> List[Tuple[str, str]]:
    """A dimenzió-érték párok rendezve (több értékű dimenzió minden értéke külön pár).

    Hívja: contrast_mining.mine."""
    return sorted((dim, value) for dim in dimensions for value in dimension_values(dimensions, dim))

def _time_dimensions(moment: datetime) -> Dict[str, str]:
    """Egy pillanat időbeli mezői (dátum, a hét napja, óra).

    Hívja: profiles._stint_profile, profiles.profile_from_timeline."""
    return {"date": moment.date().isoformat(), "weekday": _WEEKDAYS[moment.weekday()], "hour": f"{moment.hour:02d}"}

def _distinct_values(timeline: TicketTimeline, name: str) -> DimensionValue:
    """Egy mező eltérő értékei a jegy élete során.

    Hívja: profiles.profile_from_timeline."""
    values: List[str] = []
    for change in timeline.changes_of(name):
        if change.new and change.new not in values:
            values.append(change.new)
    return values[0] if len(values) == 1 else tuple(values)

def build_profile(incident: Incident, as_of: datetime, levels: Optional[Mapping[str, str]] = None) -> Optional[TicketProfile]:
    """Az incidens adatlapja az `as_of` időpontban (a későbbi eseményeket nem látja).

    A nyitás ideje az első esemény időpontja; esemény nélküli incidensre None.
    

    Hívja: compare.window_summary."""
    timeline = incident_timeline(incident, as_of)
    return profile_from_timeline(timeline, levels) if timeline is not None else None

def profile_from_timeline(timeline: TicketTimeline, levels: Optional[Mapping[str, str]] = None) -> TicketProfile:
    """A ticket adatlapja az idővonalból (mezők és mérések).

    Hívja: profiles.build_profile, profiles.build_profiles."""
    incident_id, as_of, opened_at = timeline.incident_id, timeline.as_of, timeline.opened_at
    levels = levels or {}

    dimensions: Dict[str, DimensionValue] = {}
    for name in DIMENSIONS:
        value = _distinct_values(timeline, name)
        if value:
            dimensions[name] = value
    dimensions.update(_time_dimensions(opened_at))
    current = timeline.value_at("assignment_group")
    if current:
        dimensions["current_group"] = current

    def ref(change: FieldChange, reason: str) -> Dict[str, Any]:
        return make_history_ref(incident_id, change.event, reason)

    measures: Dict[str, Optional[float]] = {}
    evidence: Dict[str, Tuple[Dict[str, Any], ...]] = {}

    # Felvétel: az első nem üres assigned_to; ha nincs, a még nyitott ticket eddigi várakozása
    pickup = next((c for c in timeline.changes_of("assigned_to") if c.new), None)
    if pickup is not None:
        measures[PICKUP_DELAY.key] = hours_between(opened_at, pickup.at)
        evidence[PICKUP_DELAY.key] = (ref(pickup, PICKUP_DELAY.key),)
    else:
        measures[PICKUP_DELAY.key] = None if timeline.first_closed_at else hours_between(opened_at, as_of)

    # Hold-idő: a hold (felfüggesztett) állapotban töltött szakaszok összege
    measures[HOLD_TIME.key] = sum(interval.hours for interval in timeline.holds)
    evidence[HOLD_TIME.key] = tuple(ref(c, HOLD_TIME.key) for c in timeline.changes_of("state") if c.new == HOLD_STATE)

    # Újraosztás: csoportváltás egyik kitöltött csoportról egy másikra; az L1-ből való továbbítás
    # a normál út, nem számít (az L1-be való visszakerülés igen)
    moves = timeline.reassignments(levels)
    measures[REASSIGNMENTS.key] = float(len(moves))
    evidence[REASSIGNMENTS.key] = tuple(ref(c, REASSIGNMENTS.key) for c in moves)

    # SLA-eszkaláció
    escalation = next((c for c in timeline.changes_of("u_sla_escalated") if c.new.lower() in SLA_ESCALATED_VALUES), None)
    measures[SLA_ESCALATION.key] = 1.0 if escalation is not None else 0.0
    evidence[SLA_ESCALATION.key] = (ref(escalation, SLA_ESCALATION.key),) if escalation is not None else ()

    return TicketProfile(incident_id, opened_at, dimensions, measures, evidence)

def stint_profiles(timeline: TicketTimeline, levels: Optional[Mapping[str, str]] = None,
                   schedules: Optional[Mapping[str, Any]] = None) -> List[TicketProfile]:
    """A ticket csoportszakaszainak adatlapjai (az L1-szakaszok nélkül, ha az L1 ismert).

    Dimenziók: a szakasz csoportja, a többi dimenzió az érkezéskori értékével, az időbeli
    dimenziók az érkezés időpontjából (pl. a szombaton érkezett, nem a szombaton nyitott ticket).
    

    Hívja: profiles.build_profiles."""
    levels = levels or {}
    schedules = schedules or {}
    return [_stint_profile(timeline, stint, schedules.get(stint.group)) for stint in timeline.stints
            if levels.get(stint.group) != L1_LEVEL]

def _stint_profile(timeline: TicketTimeline, stint: Stint, schedule: Optional[Any] = None) -> TicketProfile:
    """`schedule`: a csoport munkaideje (model.working_time.Schedule); ha ismert, a felvételi és a hold idő
    munkaórában számít (a munkaidőn kívüli várakozás nem késés).

    Hívja: profiles.stint_profiles."""
    elapsed = (lambda a, b: schedule.working_hours(a, b)) if schedule is not None else hours_between
    incident_id = timeline.incident_id
    arrival = timeline.values_at(stint.start)
    dimensions: Dict[str, DimensionValue] = {
        name: arrival[name] for name in DIMENSIONS if name != "assignment_group" and arrival.get(name)
    }
    dimensions["assignment_group"] = stint.group
    dimensions["time_basis"] = "munkaóra" if schedule is not None else "falióra"
    dimensions.update(_time_dimensions(stint.start))
    current = timeline.value_at("assignment_group")
    if current:
        dimensions["current_group"] = current

    def ref(change: FieldChange, reason: str) -> Dict[str, Any]:
        return make_history_ref(incident_id, change.event, reason)

    arrived = ref(stint.changes[0], STINT_PICKUP.key)
    pickup = stint.first_assignment
    if pickup is not None:
        pickup_hours: Optional[float] = elapsed(stint.start, pickup.at)
        pickup_evidence: Tuple[Dict[str, Any], ...] = (arrived, ref(pickup, STINT_PICKUP.key))
    else:
        # felelős nélkül továbbadott vagy lezárt szakasz: nem felvételi késés (adminisztrációs kérdés)
        pickup_hours = elapsed(stint.start, stint.end) if stint.is_open else None
        pickup_evidence = (arrived,)
    holds = tuple(ref(c, STINT_HOLD.key) for c in stint.changes_of("state") if c.new == HOLD_STATE)
    return TicketProfile(
        incident_id, stint.start, dimensions,
        {STINT_PICKUP.key: pickup_hours,
         STINT_HOLD.key: sum(elapsed(h.start, h.end) for h in stint.holds) if schedule is not None else stint.hold_hours},
        {STINT_PICKUP.key: pickup_evidence, STINT_HOLD.key: holds},
        unit_id=f"{incident_id}#{stint.index}",
    )

def build_profiles(
    incidents: Iterable[Incident], as_of: datetime, window_start: datetime,
    levels: Optional[Mapping[str, str]] = None,
    derived: Sequence[Tuple[str, str]] = (),
    schedules: Optional[Mapping[str, Any]] = None,
) -> ProfileSet:
    """Az ablakban nyitott ticketek és az ablakban kezdődött szakaszok adatlapjai az `as_of` pillanatban.

    `derived`: keresési szempontként használt almérések (dimenzió, kifejezés): az értékük (igen/nem)
    dimenzióként kerül az adatlapra, így a mintakeresés az elemző logikája szerint is bonthat.
    

    Hívja: DiscoveryService._profiles, PatternFindingDiagnosis.window_profiles, candidate_analysis.analyze_candidate,
    runner._mine_trends, runner.backfill_counts, runner.discover, runner.run_discovery."""
    extra = None
    if derived:
        from learning.children.child_runner import derived_evaluator
        extra = derived_evaluator(derived, levels or {}, schedules)
    tickets, stints = [], []
    for incident in incidents:
        timeline = incident_timeline(incident, as_of)
        if timeline is None:
            continue
        if timeline.opened_at >= window_start:
            profile = profile_from_timeline(timeline, levels)
            if extra:
                profile.dimensions.update(extra(timeline, None))
            tickets.append(profile)
        for profile in stint_profiles(timeline, levels, schedules):
            if profile.opened_at < window_start:
                continue
            if extra:
                profile.dimensions.update(extra(timeline, timeline.stints[int(profile.unit_id.rpartition("#")[2])]))
            stints.append(profile)
    return ProfileSet(tuple(tickets), tuple(stints))
