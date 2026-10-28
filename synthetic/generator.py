"""Szintetikus előtörténet valódi incidensek újramintavételezésével.

Sablon = egy valódi incidens teljes (vágás előtti) lefutása, a nyitástól mért időeltolásokkal.
Minden szintetikus nap:
- érkezés: a valódi adat azonos hétnapi ticketszáma körül (Poisson), a sablon nyitási órájával;
- sablon: egy azonos hétnapon nyitott valódi incidens, így a service–csoport–alkategória–helyszín
  összerendelések és a hét napja × óra × csoport együttjárások valósak maradnak;
- lefutás: `swap_share` valószínűséggel egy másik, azonos csoportú és hétnapú incidens lefutása,
  a sablon dimenzióértékeivel (azonos összerendelés, eltérő lefutás).

A szintetikus ticketek a valódi idősor végén ugyanúgy elvágódnak, mint a valódiak.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from models.discovery.profile import DIMENSIONS, canonical_field

TIME_KEYS = ("update_time", "sys_created_on", "timestamp", "_virtual_dt")
ID_KEYS = ("set", "incident", "incident_id", "number")
VALUE_KEYS = (("new_value", "new"), ("old_value", "old"))


def _as_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _incident_id(row: Mapping[str, Any]) -> str:
    return str(next((row[key] for key in ID_KEYS if row.get(key)), "")).strip()


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class Template:
    incident_id: str
    opened_at: datetime
    events: Tuple[Tuple[float, Dict[str, Any]], ...]  # (másodperc a nyitástól, sor időmezők nélkül)
    first_values: Dict[str, str]                       # dimenzió -> első kitöltött érték

    @property
    def weekday(self) -> int:
        return self.opened_at.weekday()

    @property
    def group(self) -> str:
        return self.first_values.get("assignment_group", "")


def build_templates(rows: Iterable[Mapping[str, Any]]) -> List[Template]:
    """A (vágás előtti) virtuális idősor sorai incidensenkénti sablonokká."""
    by_incident: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = defaultdict(list)
    for row in rows:
        moment = _as_datetime(row.get("_virtual_dt") or row.get("timestamp"))
        incident_id = _incident_id(row)
        if moment is not None and incident_id:
            clean = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items() if k not in TIME_KEYS}
            by_incident[incident_id].append((moment, clean))

    templates = []
    for incident_id, events in by_incident.items():
        events.sort(key=lambda pair: pair[0])
        opened_at = events[0][0]
        first_values: Dict[str, str] = {}
        for _, row in events:
            name = canonical_field(row.get("field"))
            value = _text(row.get("new_value") if row.get("new_value") is not None else row.get("new"))
            if name in DIMENSIONS and name not in first_values and value:
                first_values[name] = value
        templates.append(Template(
            incident_id, opened_at,
            tuple(((moment - opened_at).total_seconds(), row) for moment, row in events),
            first_values,
        ))
    return sorted(templates, key=lambda t: (t.opened_at, t.incident_id))


@dataclass(frozen=True)
class GeneratorParams:
    weeks: int = 12                 # ennyi szintetikus hét a valódi idősor elé
    seed: int = 1
    swap_share: float = 0.5         # ekkora eséllyel kap a ticket egy másik (azonos csoportú) lefutást
    open_jitter_minutes: int = 30   # a nyitási idő szórása a sablonéhoz képest (a napon belül marad)
    volume_scale: float = 1.0       # az érkezési ütem szorzója
    observed_days: int = 14         # a sablonok ennyi napról származnak (a napi átlaghoz)
    id_prefix: str = "SYN"


def _poisson(rng: random.Random, mean: float) -> int:
    """Knuth-féle Poisson-mintavétel (a napi néhány tucat ticketnél elég pontos és gyors)."""
    if mean <= 0:
        return 0
    limit, count, product = math.exp(-mean), 0, rng.random()
    while product > limit:
        count += 1
        product *= rng.random()
    return count


def _value_map(template: Template, donor: Template) -> Dict[Tuple[str, str], str]:
    """A lefutás dimenzióértékei a sablonéira cserélve: (dimenzió, donorérték) -> sablonérték."""
    return {
        (dim, donor.first_values[dim]): value
        for dim, value in template.first_values.items()
        if dim in donor.first_values and donor.first_values[dim] != value
    }


def instantiate(
    template: Template, donor: Template, incident_id: str, opened_at: datetime, cut_at: datetime
) -> List[Dict[str, Any]]:
    """Egy szintetikus incidens sorai: a donor lefutása a sablon dimenzióival, `cut_at` előtt."""
    replacements = _value_map(template, donor)
    rows = []
    for offset, source in donor.events:
        moment = (opened_at + timedelta(seconds=offset)).replace(microsecond=0)
        if moment >= cut_at:
            break
        row = dict(source)
        for key in ID_KEYS:
            if row.get(key):
                row[key] = incident_id
        stamp = moment.isoformat()
        for key in TIME_KEYS:
            row[key] = stamp
        name = canonical_field(row.get("field"))
        for keys in VALUE_KEYS:
            for key in keys:
                replacement = replacements.get((name, _text(row.get(key))))
                if replacement is not None:
                    row[key] = replacement
        row["_template"] = template.incident_id
        row["_lifecycle"] = donor.incident_id
        rows.append(row)
    return rows


def generate(
    templates: Sequence[Template], start: datetime, cut_at: datetime, params: GeneratorParams = GeneratorParams()
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """`params.weeks` hétnyi szintetikus incidens `start`-tól; a sorok és egy összesítő."""
    rng = random.Random(params.seed)
    by_weekday: Dict[int, List[Template]] = defaultdict(list)
    by_group_weekday: Dict[Tuple[str, int], List[Template]] = defaultdict(list)
    for template in templates:
        by_weekday[template.weekday].append(template)
        by_group_weekday[(template.group, template.weekday)].append(template)
    weeks_observed = params.observed_days / 7

    rows: List[Dict[str, Any]] = []
    incidents, swapped = 0, 0
    jitter = timedelta(minutes=params.open_jitter_minutes)
    for day_index in range(params.weeks * 7):
        day = (start + timedelta(days=day_index)).date()
        pool = by_weekday.get(day.weekday(), [])
        if not pool:
            continue
        day_start, day_end = datetime.combine(day, time.min), datetime.combine(day, time(23, 59, 59))
        for _ in range(_poisson(rng, len(pool) / weeks_observed * params.volume_scale)):
            template = rng.choice(pool)
            donor = template
            if rng.random() < params.swap_share:
                donor = rng.choice(by_group_weekday[(template.group, template.weekday)])
                swapped += donor is not template
            opened = datetime.combine(day, template.opened_at.time()) + rng.uniform(-1, 1) * jitter
            opened = min(max(opened, day_start), day_end)
            incidents += 1
            rows.extend(instantiate(template, donor, f"{params.id_prefix}{incidents:06d}", opened, cut_at))

    summary = {
        "start": start.isoformat(), "weeks": params.weeks, "seed": params.seed, "swap_share": params.swap_share,
        "volume_scale": params.volume_scale, "templates": len(templates), "incidents": incidents,
        "swapped_lifecycles": swapped, "rows": len(rows),
    }
    return rows, summary


def merge_timelines(*parts: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Több idősor egyesítése időrendben (az ISO időbélyegek szövegként is jól rendeződnek)."""
    def stamp(row: Mapping[str, Any]) -> str:
        value = row.get("_virtual_dt") or row.get("timestamp")
        return value.isoformat() if isinstance(value, datetime) else str(value)

    merged = [dict(row) for part in parts for row in part]
    merged.sort(key=stamp)
    return merged
