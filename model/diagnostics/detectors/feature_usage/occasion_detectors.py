"""A funkcióhasználat általános felismerői: mikor alkalom egy funkció használatára, és használták-e.
A katalógus (catalog.py) a `kind` mezővel választ közülük."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Tuple, TYPE_CHECKING

from model.timeline.field_change import FieldChange
from model.timeline.fields import HOLD_STATE
from model.timeline.stint import Stint
from model.timeline.ticket_timeline import TicketTimeline


if TYPE_CHECKING:
    from model.diagnostics.detectors.feature_usage.feature_def import FeatureDef


def _meaningful(feature: FeatureDef, changes: Iterable[FieldChange]) -> List[FieldChange]:
    return [c for c in changes if c.field in feature.fields and c.new and c.new.lower() not in feature.ignore_values]

def _hold_starts(timeline: TicketTimeline, stint: Stint) -> List[FieldChange]:
    """A csoport által indított hold-ok (state/incident_state duplikátumok nélkül)."""
    starts, seen = [], set()
    for change in timeline.own_changes(stint):
        if change.field == "state" and change.new == HOLD_STATE and change.old != HOLD_STATE and change.at not in seen:
            seen.add(change.at)
            starts.append(change)
    return starts

def _end_anchor(timeline: TicketTimeline, stint: Stint) -> FieldChange:
    """A szakasz végének eseménye: a továbbítás (a következő szakasz kezdete), a lezárás, vagy ha a
    lezárás nem ebben a szakaszban történt (lezárás utáni továbbítás), a szakasz utolsó eseménye."""
    following = timeline.next_stint(stint)
    if following is not None:
        return following.changes[0]
    closing = next((c for c in reversed(stint.changes) if c.field == "state"), None)
    return closing or stint.changes[-1]

Found = Iterable[Tuple[datetime, bool, FieldChange, Tuple[FieldChange, ...]]]

def _on_hold(feature, timeline, stint, params) -> Found:
    tolerance = timedelta(minutes=params.get("hold_tolerance_minutes", 30))
    own = _meaningful(feature, timeline.own_changes(stint))
    for start in _hold_starts(timeline, stint):
        used = tuple(c for c in own if abs(c.at - start.at) <= tolerance)
        yield start.at, bool(used), start, used

def _on_close(feature, timeline, stint, params) -> Found:
    if stint.end_reason == "closed":
        used = tuple(_meaningful(feature, timeline.own_changes(stint)))
        yield stint.end, bool(used), _end_anchor(timeline, stint), used

def _on_work(feature, timeline, stint, params) -> Found:
    if stint.is_open or (stint.first_assignment is None and stint.end_reason != "closed"):
        return
    used = tuple(_meaningful(feature, timeline.own_changes(stint)))
    yield stint.end, bool(used), _end_anchor(timeline, stint), used

def _on_transfer(feature, timeline, stint, params) -> Found:
    if stint.end_reason == "transfer":
        used = tuple(_meaningful(feature, timeline.handover_changes(stint)))
        yield stint.end, bool(used), _end_anchor(timeline, stint), used

def _assignee(feature, timeline, stint, params) -> Found:
    if not stint.is_open:
        first = stint.first_assignment
        yield stint.end, first is not None, _end_anchor(timeline, stint), (first,) if first else ()

def _receiver_sets(feature, timeline, stint, params) -> Found:
    following = timeline.next_stint(stint)
    if following is not None:
        hits = tuple(_meaningful(feature, timeline.own_changes(following)))
        yield stint.end, bool(hits), following.changes[0], hits

def _receiver_rewrites(feature, timeline, stint, params) -> Found:
    following = timeline.next_stint(stint)
    if following is None:
        return
    handed_over = timeline.values_at(following.start)
    if not any(handed_over.get(field) for field in feature.fields):
        return
    hits = tuple(c for c in timeline.own_changes(following)
                 if c.field in feature.fields and c.old and c.new and c.old != c.new)
    yield stint.end, bool(hits), following.changes[0], hits

# felismerő -> (leírás: mikor alkalom, és mi számít használatnak / bekövetkezésnek; függvény)
DETECTORS: Dict[str, Tuple[str, Callable[..., Found]]] = {
    "on_hold": ("a csoport hold állapotba tette a ticketet; a mező kitöltése a hold kezdete körül", _on_hold),
    "on_close": ("a csoport zárta le a ticketet; a mező kitöltése a szakaszban", _on_close),
    "on_work": ("a csoportnál érdemi munka folyt (volt felelős, vagy lezárták); a mező kitöltése a szakaszban", _on_work),
    "on_transfer": ("a csoport továbbította a ticketet; a mező kitöltése a továbbítás mentésében", _on_transfer),
    "assignee": ("a szakasz véget ért; volt-e felelőse a csoportnál", _assignee),
    "receiver_sets": ("a csoport továbbította a ticketet; a fogadó kitöltötte a mezőt (pl. hibás továbbítás jelzése)",
                      _receiver_sets),
    "receiver_rewrites": ("a csoport kitöltött mezővel továbbított; a fogadó átírta", _receiver_rewrites),
}
