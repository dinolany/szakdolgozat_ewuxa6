"""Hibák beültetése a szintetikus idősorba, ground truth-szal (a backtest ezekhez méri a felismerést).

Egy forgatókönyv (Fault) egy csoportot (vagy mindenkit) érint egy időszakban, hirtelen (step) vagy
fokozatosan erősödve (ramp). Egy jogosult szakasz (a csoporthoz érkezés az időszakban) a pillanatnyi
intenzitás valószínűségével kapja a hibát:

- slow_pickup:  a csoportnál az első felvétel `delay_hours`-szal később történik;
- ping_pong:    a csoport továbbadja egy másik csoportnak, az visszaküldi (`bounce_hours` alatt);
- long_hold:    a csoport `hold_hours` órára holdba teszi (indoklással, hogy csak a hold hossza legyen hiba);
- drop_feature: a csoport szakaszaiból eltűnnek a megadott mezők változásai (pl. work_notes).

Az időbeli beszúrásnál a ticket minden későbbi eseménye eltolódik, így a lefutás konzisztens marad;
a valódi idősor végén ugyanúgy elvágódik, mint a többi szintetikus ticket.
"""

import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from models.timeline import canonical_field, clean_value
from synthetic.generator import TIME_KEYS

ALL = "*"


@dataclass
class Fault:
    name: str
    kind: str                      # slow_pickup | ping_pong | long_hold | drop_feature
    group: str = "auto"            # csoport sys_id, "auto" (a következő nagy rezolver csoport) vagy "*" (mindenki)
    shape: str = "step"            # step | ramp
    start_week: float = 6          # a szintetikus időszak kezdetétől
    weeks: float = 4
    intensity: float = 0.8         # a jogosult szakaszok ennyi része kapja (ramp: a végére éri el)
    delay_hours: float = 24        # slow_pickup
    bounce_hours: float = 2        # ping_pong
    hold_hours: float = 48         # long_hold
    fields: List[str] = field(default_factory=lambda: ["work_notes"])  # drop_feature

    def probability(self, moment: datetime, start: datetime, end: datetime) -> float:
        if not start <= moment < end:
            return 0.0
        if self.shape == "ramp":
            return self.intensity * (moment - start).total_seconds() / max(1.0, (end - start).total_seconds())
        return self.intensity


DEFAULT_SCENARIOS: Tuple[Fault, ...] = (
    Fault("lassulo_felvetel", "slow_pickup", shape="ramp", start_week=6, weeks=5, intensity=0.9, delay_hours=20),
    Fault("hirtelen_ping_pong", "ping_pong", shape="step", start_week=8, weeks=3, intensity=0.6),
    Fault("hosszu_hold", "long_hold", shape="step", start_week=7, weeks=3, intensity=0.5, hold_hours=60),
    Fault("elhagyott_munkajegyzet", "drop_feature", shape="step", start_week=5, weeks=6, intensity=1.0),
    Fault("rendszerszintu_lassulas", "slow_pickup", group=ALL, shape="step", start_week=10, weeks=1, intensity=0.3,
          delay_hours=12),
)


def load_scenarios(path: Optional[str]) -> List[Fault]:
    if not path:
        return [Fault(**asdict(f)) for f in DEFAULT_SCENARIOS]
    return [Fault(**entry) for entry in json.loads(Path(path).read_text(encoding="utf-8"))]


# --------------------------------------------------------------------------
# Segédek a sorokon
# --------------------------------------------------------------------------

def _time(row: Dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["_virtual_dt"]))


def _set_time(row: Dict[str, Any], moment: datetime) -> None:
    stamp = moment.replace(microsecond=0).isoformat()
    for key in TIME_KEYS:
        row[key] = stamp


def _new(row: Dict[str, Any]) -> str:
    return clean_value(row.get("new_value") if row.get("new_value") is not None else row.get("new"))


def _old(row: Dict[str, Any]) -> str:
    return clean_value(row.get("old_value") if row.get("old_value") is not None else row.get("old"))


def _event(template: Dict[str, Any], field_name: str, old: str, new: str, moment: datetime, fault: str) -> Dict[str, Any]:
    row = {key: template.get(key) for key in ("set", "incident", "number", "_template", "_lifecycle") if key in template}
    row.update({"field": field_name, "old_value": old, "new_value": new, "old": old, "new": new, "_fault": fault})
    _set_time(row, moment)
    return row


def _shift_from(rows: List[Dict[str, Any]], pivot: datetime, delta: timedelta) -> None:
    for row in rows:
        moment = _time(row)
        if moment >= pivot:
            _set_time(row, moment + delta)


def _arrivals(rows: Sequence[Dict[str, Any]], group: str) -> List[Tuple[int, datetime, Optional[datetime]]]:
    """A csoporthoz érkezések: (sorindex, érkezés, a szakasz vége vagy None)."""
    result, current = [], ""
    group_rows = [(i, _time(r), _new(r)) for i, r in enumerate(rows) if canonical_field(r.get("field")) == "assignment_group" and _new(r)]
    for position, (index, moment, value) in enumerate(group_rows):
        if value != current and (group == ALL or value == group):
            following = next((m for _, m, v in group_rows[position + 1:] if v != value), None)
            result.append((index, moment, following))
        current = value
    return result


# --------------------------------------------------------------------------
# Hibák
# --------------------------------------------------------------------------

def _slow_pickup(rows, arrival_index, arrival, stint_end, fault, rng, others):
    pickup = next((r for r in rows[arrival_index + 1:] if canonical_field(r.get("field")) == "assigned_to" and _new(r)
                   and _time(r) >= arrival and (stint_end is None or _time(r) < stint_end)), None)
    if pickup is None:
        return None
    pivot = _time(pickup)
    _shift_from(rows, pivot, timedelta(hours=fault.delay_hours))
    pickup["_fault"] = fault.name
    return {"pickup_was": pivot.isoformat(), "delay_hours": fault.delay_hours}


def _ping_pong(rows, arrival_index, arrival, stint_end, fault, rng, others):
    group = _new(rows[arrival_index])
    candidates = [g for g in others if g != group]
    if not candidates:
        return None
    other = rng.choice(candidates)
    pivot = arrival + timedelta(minutes=20)
    if stint_end is not None and pivot >= stint_end:
        return None
    bounce = timedelta(hours=fault.bounce_hours)
    _shift_from(rows, pivot, bounce + timedelta(minutes=1))
    template = rows[arrival_index]
    rows.append(_event(template, "assignment_group", group, other, pivot, fault.name))
    rows.append(_event(template, "assignment_group", other, group, pivot + bounce, fault.name))
    return {"bounced_to": other}


def _long_hold(rows, arrival_index, arrival, stint_end, fault, rng, others):
    pivot = arrival + timedelta(minutes=30)
    if stint_end is not None and pivot >= stint_end:
        return None
    previous = next((_new(r) for r in reversed(rows) if canonical_field(r.get("field")) == "state" and _time(r) <= pivot), "") or "2"
    if previous in {"3", "6", "7"}:
        return None
    hold = timedelta(hours=fault.hold_hours)
    _shift_from(rows, pivot, hold + timedelta(minutes=1))
    template = rows[arrival_index]
    for field_name in ("state", "incident_state"):
        rows.append(_event(template, field_name, previous, "3", pivot, fault.name))
        rows.append(_event(template, field_name, "3", previous, pivot + hold, fault.name))
    rows.append(_event(template, "u_pending_reason", "", "100", pivot, fault.name))
    return {"hold_hours": fault.hold_hours}


def _drop_feature(rows, arrival_index, arrival, stint_end, fault, rng, others):
    fields = set(fault.fields)
    removed = [r for r in rows if canonical_field(r.get("field")) in fields and _time(r) > arrival
               and (stint_end is None or _time(r) <= stint_end)]
    if not removed:
        return None
    for row in removed:
        rows.remove(row)
    return {"removed": len(removed)}


APPLY = {"slow_pickup": _slow_pickup, "ping_pong": _ping_pong, "long_hold": _long_hold, "drop_feature": _drop_feature}


def group_profiles(rows: Sequence[Dict[str, Any]], l1_groups: Sequence[str] = (), as_of: Optional[datetime] = None) -> Dict[str, Dict[str, float]]:
    """Csoportonként a természetes (hiba nélküli) arányok a rezolver szakaszokon: lassú felvétel,
    hosszú hold, továbbpattanás, munkajegyzet-használat. Ebből választható "tiszta" célcsoport:
    ahol a beültetett hiba nem keveredik egy már meglévő, természetes mintával."""
    from synthetic.compare import incidents_from_rows   # körkörös import elkerülése
    from models.timeline import incident_timeline
    incidents = incidents_from_rows(rows)
    moment = as_of or max(_time(r) for r in rows)
    stats: Dict[str, Counter] = defaultdict(Counter)
    for incident in incidents:
        timeline = incident_timeline(incident, moment)
        if timeline is None:
            continue
        for stint in timeline.stints:
            if stint.group in l1_groups or stint.is_open:
                continue
            c = stats[stint.group]
            c["stints"] += 1
            pickup = stint.first_assignment
            c["slow_pickup"] += pickup is None or (pickup.at - stint.start).total_seconds() >= 8 * 3600
            c["long_hold"] += stint.hold_hours >= 24
            following = timeline.next_stint(stint)
            c["ping_pong"] += following is not None and following.group not in l1_groups and timeline.next_stint(following) is not None
            worked = stint.first_assignment is not None or stint.end_reason == "closed"
            c["worked"] += worked
            c["drop_feature"] += worked and any(ch.field == "work_notes" and ch.new for ch in timeline.own_changes(stint))
    return {group: {"stints": c["stints"], **{key: c[key] / max(1, c["worked" if key == "drop_feature" else "stints"])
                                              for key in ("slow_pickup", "long_hold", "ping_pong", "drop_feature")}}
            for group, c in stats.items()}


def pick_clean_group(kind: str, profiles: Mapping[str, Mapping[str, float]], used: set, min_stints: int = 80) -> Optional[str]:
    """A fajtához illő tiszta célcsoport: elég nagy, és a természetes aránya a legkedvezőbb
    (felvétel/hold/ping-pong: a legalacsonyabb; munkajegyzet: a leggyakoribb használat)."""
    candidates = [(g, p) for g, p in profiles.items() if p["stints"] >= min_stints and g not in used]
    if not candidates:
        return None
    if kind == "drop_feature":
        return max(candidates, key=lambda gp: (gp[1]["drop_feature"], gp[1]["stints"]))[0]
    return min(candidates, key=lambda gp: (gp[1][kind], -gp[1]["stints"]))[0]


def rank_groups(rows: Sequence[Dict[str, Any]], exclude: Sequence[str] = ()) -> List[str]:
    """A csoportok az érkezések száma szerint (a nagyobb csoportnál több adatból látszik a hiba)."""
    counts: Counter = Counter()
    for row in rows:
        if canonical_field(row.get("field")) == "assignment_group" and _new(row) and _new(row) != _old(row):
            counts[_new(row)] += 1
    return [group for group, _ in counts.most_common() if group not in exclude]


def inject(
    rows: List[Dict[str, Any]],
    scenarios: Sequence[Fault],
    synthetic_start: datetime,
    cut_at: datetime,
    l1_groups: Sequence[str] = (),
    seed: int = 1,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """A forgatókönyvek beültetése a (szintetikus) sorokba; az új sorlista és a ground truth."""
    rng = random.Random(seed)
    ranked = rank_groups(rows, exclude=l1_groups)
    profiles = group_profiles(rows, l1_groups) if any(f.group == "auto" for f in scenarios) else {}
    used: set = set()
    resolved = []
    for fault in scenarios:
        group = fault.group
        if group == "auto":
            group = pick_clean_group(fault.kind, profiles, used) or next(g for g in ranked if g not in used)
        used.add(group)
        start = synthetic_start + timedelta(weeks=fault.start_week)
        resolved.append((fault, group, start, start + timedelta(weeks=fault.weeks)))

    by_incident: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_incident[str(row.get("set"))].append(dict(row))
    truth: List[Dict[str, Any]] = []
    for incident_id, incident_rows in by_incident.items():
        incident_rows.sort(key=_time)
        for fault, group, start, end in resolved:
            for arrival_index, arrival, stint_end in _arrivals(incident_rows, group):
                arrived_to = _new(incident_rows[arrival_index])
                if group == ALL and arrived_to in l1_groups:
                    continue  # mindenkire vonatkozó hiba: a rezolver szakaszok
                if rng.random() >= fault.probability(arrival, start, end):
                    continue
                info = APPLY[fault.kind](incident_rows, arrival_index, arrival, stint_end, fault, rng, ranked[:20])
                incident_rows.sort(key=_time)
                if info is not None:
                    truth.append({"fault": fault.name, "kind": fault.kind, "group": arrived_to,
                                  "incident_id": incident_id, "arrival": arrival.isoformat(), **info})
                if fault.kind != "drop_feature":
                    break  # ticketenként és forgatókönyvenként egy beültetés (az első jogosult érkezés);
                # a funkció elhagyása szokás: a csoport minden szakaszát érinti, a visszatérőket is
    result = [row for incident_rows in by_incident.values() for row in incident_rows if _time(row) < cut_at]
    manifest = {
        "seed": seed,
        "scenarios": [{**asdict(fault), "group": group, "start": start.isoformat(), "end": end.isoformat(),
                       "applied": sum(1 for t in truth if t["fault"] == fault.name),
                       "natural": profiles.get(group, {})}
                      for fault, group, start, end in resolved],
        "truth": truth,
    }
    return result, manifest
