"""Hasonlít-e a szintetikus idősor a valódira: ugyanazok a mérések két azonos hosszú ablakon."""

from collections import Counter
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from models.discovery.measures import MEASURES
from models.discovery.profile import TicketProfile, build_profile, dimension_values
from models.history import HistoryLine
from models.incident import Incident

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def incidents_from_rows(rows: Iterable[Mapping[str, Any]]) -> List[Incident]:
    incidents: Dict[str, Incident] = {}
    for row in sorted(rows, key=lambda r: str(r["_virtual_dt"])):
        incident_id = row.get("set") or row.get("incident")
        incidents.setdefault(incident_id, Incident(incident_id)).apply_event(HistoryLine(dict(row)))
    return list(incidents.values())


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else float("nan")


def window_summary(incidents: Iterable[Incident], window_start: datetime, as_of: datetime) -> Dict[str, Any]:
    """Az ablakban nyitott ticketek összesítése az `as_of` pillanatban (mint a feltárásnál)."""
    profiles: List[TicketProfile] = [
        p for incident in incidents
        if (p := build_profile(incident, as_of)) is not None and window_start <= p.opened_at <= as_of
    ]
    days = max(1.0, (as_of - window_start).total_seconds() / 86400)
    weekdays = Counter(_WEEKDAYS[p.opened_at.weekday()] for p in profiles)
    summary: Dict[str, Any] = {
        "incidents": len(profiles),
        "per_day": len(profiles) / days,
        "weekday_share": {day: weekdays[day] / max(1, len(profiles)) for day in _WEEKDAYS},
        # a nyitáskori csoport (a ticket útjának első csoportja)
        "groups": Counter((dimension_values(p.dimensions, "assignment_group") or ("",))[0] for p in profiles),
        "measures": {},
    }
    for measure in (m for m in MEASURES if m.scope == "ticket"):
        values = [p.measures[measure.key] for p in profiles if p.measures.get(measure.key) is not None]
        summary["measures"][measure.key] = {
            "applicable": len(values) / max(1, len(profiles)),
            "bad_rate": sum(measure.is_bad(v) for v in values) / max(1, len(values)),
            "median": _quantile(values, 0.5),
            "p90": _quantile(values, 0.9),
        }
    return summary


def report(real: Mapping[str, Any], synthetic: Mapping[str, Any], top_groups: int = 8) -> str:
    lines = [f"{'':34}{'valódi':>12}{'szintetikus':>14}",
             f"{'incidens az ablakban':34}{real['incidents']:>12}{synthetic['incidents']:>14}",
             f"{'naponta':34}{real['per_day']:>12.1f}{synthetic['per_day']:>14.1f}"]
    for day in _WEEKDAYS:
        lines.append(f"{'  ' + day + ' arány':34}{real['weekday_share'][day]:>12.1%}{synthetic['weekday_share'][day]:>14.1%}")
    lines.append("csoportok (a valódi leggyakoribbjai):")
    real_total, synthetic_total = max(1, real["incidents"]), max(1, synthetic["incidents"])
    for group, count in real["groups"].most_common(top_groups):
        name = (group or "(üres)")[:28]
        lines.append(f"{'  ' + name:34}{count / real_total:>12.1%}{synthetic['groups'].get(group, 0) / synthetic_total:>14.1%}")
    lines.append("mérések (mérhető arány / rossz arány / medián / p90):")
    for key, r in real["measures"].items():
        s = synthetic["measures"][key]
        lines.append(f"  {key}")
        for label, fmt in (("applicable", ".1%"), ("bad_rate", ".1%"), ("median", ".2f"), ("p90", ".2f")):
            lines.append(f"{'    ' + label:34}{r[label]:>12{fmt}}{s[label]:>14{fmt}}")
    return "\n".join(lines)
