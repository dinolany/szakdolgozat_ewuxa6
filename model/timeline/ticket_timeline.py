"""Egy ticket útja a history-ból, egy adott pillanatig (`as_of`) nézve.

A diagnosztikák ebből dolgoznak, nem az incidens éppen beírt mezőiből: a history-ban minden
benne van (ki, mikor, mit változtatott), és bármely korábbi pillanat állapota visszaállítható.

Fogalmak:
- változás (FieldChange): egy history sor egységesített mezőnévvel és értékekkel;
- szakasz (Stint): az az időszak, amíg a ticket egy megoldócsoportnál volt; a csoportra vonatkozó
  mérések (felvétel, hold, adminisztráció) szakaszonként értelmezendők, nem a teljes ticketen;
- az `as_of` utáni események nem léteznek: a még tartó szakasz és hold az `as_of`-ig tart.
"""

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from model.diagnostics.core.window import parse_timestamp
from model.history_line import HistoryLine
from model.knowledge.knowledge_base import L1_LEVEL

FIELD_ALIASES = {
    "u_assignment_group": "assignment_group",
    "u_business_service": "business_service",
    "service": "business_service",
    "incident_state": "state",
}
EMPTY_VALUES = {"", "n/a", "none", "null"}
HOLD_STATE = "3"
CLOSED_STATES = frozenset({"6", "7"})


def canonical_field(raw: Any) -> str:
    """A history mezőneve egységesítve (pl. u_assignment_group -> assignment_group)."""
    name = str(raw or "").strip().lower()
    return FIELD_ALIASES.get(name, name)


def clean_value(raw: Any) -> str:
    text = str(raw).strip() if raw is not None else ""
    return "" if text.lower() in EMPTY_VALUES else text


def hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600)


@dataclass(frozen=True)
class FieldChange:
    at: datetime
    field: str          # egységesített mezőnév
    old: str
    new: str
    event: HistoryLine  # az eredeti history sor (bizonyítéknak)


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    open: bool = False  # az `as_of` pillanatában még tart

    @property
    def hours(self) -> float:
        return hours_between(self.start, self.end)

    def clip(self, start: datetime, end: datetime) -> Optional["Interval"]:
        lo, hi = max(self.start, start), min(self.end, end)
        return Interval(lo, hi, self.open and hi == self.end) if lo < hi else None


@dataclass(frozen=True)
class Stint:
    """Egy csoportnál töltött szakasz: [start, end), az `as_of`-ig még tartó szakasznál end = as_of."""

    index: int
    group: str
    start: datetime
    end: datetime
    end_reason: str                     # transfer | closed | open
    next_group: str                     # továbbításnál a következő csoport
    changes: Tuple[FieldChange, ...]    # a szakasz alatti változások (a kezdő csoportváltással együtt)
    holds: Tuple[Interval, ...]         # a szakaszra vágott hold-időszakok
    # Az érkezés mentésének minden változása, a naplóbeli sorrendtől függetlenül (azonos időbélyegű
    # sorok sorrendje esetleges: a felelős törlése vagy beállítása a csoportváltás előtt is állhat)
    arrival: Tuple[FieldChange, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.end_reason == "open"

    @property
    def hours(self) -> float:
        return hours_between(self.start, self.end)

    @property
    def hold_hours(self) -> float:
        return sum(interval.hours for interval in self.holds)

    @property
    def first_assignment(self) -> Optional[FieldChange]:
        """Az első felelőshöz rendelés a szakaszban (a csoporton belüli felvétel).

        A továbbítás mentésében beállított felelős a fogadó csoporté (a küldő a fogadó egy tagjára tette):
        így az érkezés mentése ide számít, a saját továbbító mentése viszont nem."""
        pool = list(self.arrival) + [c for c in self.changes if c.at > self.start]
        transfer = self.end_reason == "transfer"
        return next((c for c in pool if c.field == "assigned_to" and c.new and not (transfer and c.at >= self.end)), None)

    def changes_of(self, field: str) -> List[FieldChange]:
        return [c for c in self.changes if c.field == field]


@dataclass(frozen=True)
class TicketTimeline:
    incident_id: str
    as_of: datetime
    changes: Tuple[FieldChange, ...]    # időrendben (azonos időpontnál a history sorrendjében)
    stints: Tuple[Stint, ...]
    holds: Tuple[Interval, ...]

    @property
    def opened_at(self) -> datetime:
        return self.changes[0].at

    @property
    def state(self) -> str:
        return self.value_at("state")

    @property
    def is_closed(self) -> bool:
        """Az `as_of` pillanatában lezárt (megoldott vagy zárt) állapotban van."""
        return self.state in CLOSED_STATES

    @property
    def first_closed_at(self) -> Optional[datetime]:
        """Az első lezárás ideje (újranyitás után is megmarad)."""
        return next((c.at for c in self.changes if c.field == "state" and c.new in CLOSED_STATES), None)

    @property
    def groups(self) -> List[str]:
        """Az érintett csoportok az első érintés sorrendjében (ismétlés nélkül)."""
        seen: List[str] = []
        for stint in self.stints:
            if stint.group not in seen:
                seen.append(stint.group)
        return seen

    def changes_of(self, field: str) -> List[FieldChange]:
        return [c for c in self.changes if c.field == field]

    def reassignments(self, levels: Mapping[str, str] = {}) -> List[FieldChange]:
        """Csoportváltások egyik kitöltött csoportról egy másikra. Az L1-ből való továbbítás a normál
        út, nem számít (ha a szintek ismertek); az L1-be való visszakerülés igen."""
        return [
            c for c in self.changes_of("assignment_group")
            if c.old and c.new and c.old != c.new
            and not (levels.get(c.old) == L1_LEVEL and levels.get(c.new) != L1_LEVEL)
        ]

    def first_value(self, field: str) -> str:
        """Az első kitöltött érték (pl. a nyitáskori csoport)."""
        return next((c.new for c in self.changes if c.field == field and c.new), "")

    def value_at(self, field: str, moment: Optional[datetime] = None) -> str:
        """A mező értéke egy pillanatban (az addig, azzal együtt történt változások után)."""
        moment = moment or self.as_of
        value = ""
        for change in self.changes:
            if change.at > moment:
                break
            if change.field == field:
                value = change.new
        return value

    def values_at(self, moment: Optional[datetime] = None, before: bool = False) -> Dict[str, str]:
        """Minden mező értéke egy pillanatban; `before`: a pillanatban történt változások nélkül
        (pl. egy szakasz végén az átadás előtti állapot)."""
        moment = moment or self.as_of
        values: Dict[str, str] = {}
        for change in self.changes:
            if change.at > moment or (before and change.at == moment):
                break
            values[change.field] = change.new
        return values

    def next_stint(self, stint: Stint) -> Optional[Stint]:
        return self.stints[stint.index + 1] if stint.index + 1 < len(self.stints) else None

    def handover_changes(self, stint: Stint) -> List[FieldChange]:
        """A továbbítással egy mentésben történt változások (a csoportváltás pillanatában), a
        csoportváltás nélkül. Ezeket a küldő csoport végezte, bár időben a következő szakaszba esnek."""
        following = self.next_stint(stint)
        if following is None:
            return []
        return [c for c in following.changes if c.at == following.start and c.field != "assignment_group"]

    def own_changes(self, stint: Stint) -> List[FieldChange]:
        """A csoport saját változásai a szakaszban: az érkezés pillanatának (a küldő vagy a ticket
        létrehozása által végzett) változásai nélkül, a saját továbbítási mentésével együtt."""
        return [c for c in stint.changes if c.at > stint.start] + self.handover_changes(stint)

    def stint_at(self, moment: datetime) -> Optional[Stint]:
        return next((s for s in self.stints if s.start <= moment < s.end or (s.is_open and moment >= s.start)), None)


def _changes(history: Sequence[HistoryLine], as_of: datetime) -> List[FieldChange]:
    changes = []
    for event in history:
        moment = parse_timestamp(event.timestamp)
        if moment is not None and moment <= as_of:
            changes.append(FieldChange(moment, canonical_field(event.field),
                                       clean_value(event.old), clean_value(event.new), event))
    changes.sort(key=lambda c: c.at)  # stabil: azonos időpontnál a history sorrendje marad
    return changes


def _holds(changes: Sequence[FieldChange], as_of: datetime) -> List[Interval]:
    holds, since = [], None
    for change in changes:
        if change.field != "state":
            continue
        if since is not None and change.new != HOLD_STATE:
            holds.append(Interval(since, change.at))
            since = None
        elif since is None and change.new == HOLD_STATE:
            since = change.at
    if since is not None:
        holds.append(Interval(since, as_of, open=True))
    return holds


def _stints(changes: Sequence[FieldChange], holds: Sequence[Interval], as_of: datetime) -> List[Stint]:
    # A csoport határai: minden olyan csoportváltás, amely egy másik (kitöltött) csoportra visz
    boundaries: List[Tuple[int, datetime, str]] = []
    current = ""
    for position, change in enumerate(changes):
        if change.field == "assignment_group" and change.new and change.new != current:
            boundaries.append((position, change.at, change.new))
            current = change.new
    stints = []
    for index, (position, start, group) in enumerate(boundaries):
        following = boundaries[index + 1] if index + 1 < len(boundaries) else None
        end_position = following[0] if following else len(changes)
        end = following[1] if following else as_of
        segment = tuple(changes[position:end_position])
        if following:
            reason, next_group = "transfer", following[2]
        else:
            last_state = next((c.new for c in reversed(changes) if c.field == "state"), "")
            reason, next_group = ("closed" if last_state in CLOSED_STATES else "open"), ""
            if reason == "closed":
                # a lezárás utáni adminisztráció (pl. close_notes) is a lezáró szakasz változásai közt marad
                # (ha a csoportváltás a lezárás után történt, a szakasz a csoportváltáskor kezdődik és ér véget)
                end = max(start, next(c.at for c in reversed(changes) if c.field == "state"))
        clipped = tuple(c for h in holds if (c := h.clip(start, end)) is not None)
        arrival = tuple(c for c in changes if c.at == start and c.field != "assignment_group")
        stints.append(Stint(index, group, start, end, reason, next_group, segment, clipped, arrival))
    return stints


def build_timeline(incident_id: str, history: Sequence[HistoryLine], as_of: datetime) -> Optional[TicketTimeline]:
    """A ticket idővonala az `as_of` pillanatig; None, ha addig nincs eseménye."""
    return _from_changes(str(incident_id), _changes(history, as_of), as_of)


def _from_changes(incident_id: str, changes: Sequence[FieldChange], as_of: datetime) -> Optional[TicketTimeline]:
    if not changes:
        return None
    changes = list(changes)
    holds = _holds(changes, as_of)
    return TicketTimeline(incident_id, as_of, tuple(changes), tuple(_stints(changes, holds, as_of)), tuple(holds))


_CACHE_ATTRIBUTE = "_timeline_cache"


def incident_timeline(incident: Any, as_of: datetime) -> Optional[TicketTimeline]:
    """Az incidens idővonala az `as_of` pillanatig.

    A history egyszer értelmeződik (időrendezett változáslista az incidensen tárolva, amíg a history
    hossza nem változik), és mérésenként csak az `as_of`-ig tartó részéből épül az idővonal; az utolsó
    néhány `as_of` idővonala is megmarad, mert egy mérési pillanatban több diagnosztika is kéri.
    """
    history = incident.history
    cache = getattr(incident, _CACHE_ATTRIBUTE, None)
    if cache is None or cache["length"] != len(history):
        changes = _changes(history, datetime.max)
        cache = {"length": len(history), "changes": changes, "times": [c.at for c in changes], "built": {}}
        try:
            setattr(incident, _CACHE_ATTRIBUTE, cache)
        except AttributeError:  # pl. __slots__-os objektum: gyorsítótár nélkül
            pass
    built = cache["built"]
    if as_of not in built:
        if len(built) >= 4:
            built.pop(next(iter(built)))
        built[as_of] = _from_changes(str(incident.id), cache["changes"][:bisect_right(cache["times"], as_of)], as_of)
    return built[as_of]
