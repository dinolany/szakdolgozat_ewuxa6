"""Backlog-naptár: egy csoport várakozó (felvetlen) jegyei napok × órák szerint, rajta a talált mintákkal.

A naptár a nyomozás helye: ránézésre látszik, hol a torlódás csúcsa, a csoport munkaidején belül vagy kívül,
és hogy a gép milyen mintát talált rá (a minta által leírt napok / órák, és az érintett esetek érkezése).

Mérések óránként (az óra közepén mintavételezve):
- waiting: a csoportnál felvetlenül várakozó szakaszok (megérkezett, még nincs felelőse, és nem ment tovább);
- open: a csoportnál nyitott szakaszok (felelőstől függetlenül);
- arrivals: az órában a csoporthoz érkezett szakaszok.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from learning.mining.contrast_mining import with_implied


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def group_stints(timelines: Iterable, group: str, since: datetime, until: datetime) -> List[Dict[str, Any]]:
    """A csoport szakaszai, amelyek az időszakot érintik: érkezés, felvétel, vég.

    Hívja: DiscoveryService.backlog_calendar."""
    result = []
    for timeline in timelines:
        for stint in timeline.stints:
            if stint.group != group or stint.start > until or stint.end < since:
                continue
            pickup = stint.first_assignment
            result.append({"incident_id": timeline.incident_id, "index": stint.index, "start": stint.start,
                           "pickup": pickup.at if pickup else None, "end": stint.end})
    return result

def calendar(stints: Sequence[Mapping[str, Any]], since: datetime, days: int) -> List[Dict[str, Any]]:
    """Napok × órák cellái: várakozó, nyitott, érkezett.

    Hívja: DiscoveryService.backlog_calendar."""
    start_day = since.replace(hour=0, minute=0, second=0, microsecond=0)
    arrivals: Dict[tuple, int] = defaultdict(int)
    for s in stints:
        key = (s["start"].date().isoformat(), s["start"].hour)
        arrivals[key] += 1
    cells = []
    for d in range(days):
        day = start_day + timedelta(days=d)
        for hour in range(24):
            moment = day + timedelta(hours=hour, minutes=30)
            waiting = open_ = 0
            for s in stints:
                if s["start"] <= moment < s["end"]:
                    open_ += 1
                    if s["pickup"] is None or moment < s["pickup"]:
                        waiting += 1
            cells.append({"day": day.date().isoformat(), "hour": hour, "waiting": waiting, "open": open_,
                          "arrivals": arrivals.get((day.date().isoformat(), hour), 0)})
    return cells

def time_marks(items: Sequence[Sequence[str]]) -> Dict[str, List[Any]]:
    """Amit a minta időben leír: dátumok, a hét napjai (0 = hétfő), órák.

    Hívja: DiscoveryService.backlog_calendar."""
    marks: Dict[str, List[Any]] = {"dates": [], "weekdays": [], "hours": []}
    for dim, value in items:
        if dim == "date":
            marks["dates"].append(value)
        elif dim == "weekday" and value in _WEEKDAYS:
            marks["weekdays"].append(_WEEKDAYS.index(value))
        elif dim == "hour":
            marks["hours"].append(int(value))
    return marks

def candidate_groups(candidate: Any) -> set:
    """A jelölt mintájában szereplő csoportok (a naptár csoportválasztójához és a jelöltek csoporthoz rendeléséhez).

    Hívja: DiscoveryService.backlog_calendar."""
    return {v for desc in candidate.descriptions() for d, v in with_implied(tuple(sorted(desc))) if d == "assignment_group"}

def affected_arrivals(candidate: Any, stints: Sequence[Mapping[str, Any]], scope: str, since: datetime,
                      timelines_by_id: Mapping[str, Any]) -> List[str]:
    """A jelölt érintett eseteinek érkezése a csoporthoz (szakaszos mérés) vagy a jegy nyitása (ticketszintű).

    Hívja: DiscoveryService.backlog_calendar."""
    bad = set(candidate.bad_incident_ids)
    if scope == "stint":
        return sorted(s["start"].isoformat() for s in stints if s["incident_id"] in bad and s["start"] >= since)
    result = []
    for incident_id in bad:
        timeline = timelines_by_id.get(incident_id)
        if timeline is not None and timeline.opened_at >= since and any(s["incident_id"] == incident_id for s in stints):
            result.append(timeline.opened_at.isoformat())
    return sorted(result)
