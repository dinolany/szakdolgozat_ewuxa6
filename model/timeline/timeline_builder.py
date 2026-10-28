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
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from model.diagnostics.core.measurement_window import parse_timestamp
from model.history_line import HistoryLine
from model.timeline.field_change import FieldChange
from model.timeline.fields import CLOSED_STATES, HOLD_STATE, canonical_field, clean_value
from model.timeline.interval import Interval
from model.timeline.stint import Stint
from model.timeline.ticket_timeline import TicketTimeline


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
