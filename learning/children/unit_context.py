from datetime import datetime
from typing import Any, Mapping, Optional, Tuple

from model.timeline.stint import Stint
from model.timeline.ticket_timeline import TicketTimeline
from model.working_time.schedule import Schedule


UnitRef = Tuple[str, Optional[int]]   # (incidens, szakasz sorszáma; ticket egységnél None)

def _minutes(hours: float) -> float:
    """Órából percet számol (két tizedesre kerekítve).

    Hívja: children._var_felvetel_munkaperc, children._var_felvetel_perc, children._var_felvetel_utan_perc,
    children._var_hold_perc, children._var_jegy_perc, children._var_szakasz_munkaperc, children._var_szakasz_perc."""
    return round(hours * 60, 2)

class UnitContext:
    """Egy eset (szakasz vagy ticket) a kifejezés szemszögéből."""

    def __init__(self, timeline: TicketTimeline, stint: Optional[Stint], levels: Mapping[str, str],
                 schedules: Optional[Mapping[str, Schedule]] = None):
        """Egy eset (ticket, vagy szakasz: egy csoportnál töltött idő) a kifejezés-értelmező számára: az idővonal, a
        szakasz, a csoportszintek és a munkaidők."""
        self.timeline, self.stint, self.levels = timeline, stint, levels
        self.schedules = schedules or {}

    @property
    def end(self) -> datetime:
        """Az eset vége: a szakasz vége, ticketnél az első lezárás vagy a mérés pillanata."""
        if self.stint is not None:
            return self.stint.end
        return self.timeline.first_closed_at or self.timeline.as_of

    @property
    def start(self) -> datetime:
        """Az eset kezdete: a szakasz kezdete, ticketnél a nyitás."""
        return self.stint.start if self.stint is not None else self.timeline.opened_at

    def own_changes(self):
        """Az eset saját változásai: szakasznál a csoport által tett mentések, ticketnél mind.

        Hívja: children._fn_kitoltve, children._fn_valtozasok, children._interactions,
        children._var_elso_lepes_felvetel_utan."""
        if self.stint is None:
            return list(self.timeline.changes)
        return self.timeline.own_changes(self.stint)

    def _closing_stint(self) -> Optional[Stint]:
        """A jegyet lezáró szakasz (vagy None, ha nincs lezárva).

        Hívja: children._var_zaro_csoport."""
        return next((s for s in reversed(self.timeline.stints) if s.end_reason == "closed"), None)

def unit_ref(profile: Any) -> UnitRef:
    """Adatlap -> eset hivatkozás (szakasznál "<incidens>#<sorszám>").

    Hívja: DiscoveryService._unit_sets."""
    unit_id = getattr(profile, "unit_id", "") or ""
    if "#" in unit_id:
        incident_id, _, index = unit_id.rpartition("#")
        return incident_id, int(index)
    return profile.incident_id, None
