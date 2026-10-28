"""Egy jelölt (vagy finding) elemzése döntéshez: mit mér, mi köti össze a rossz eseteket, és mi a trend.

Az elemző felület ebből dolgozik. Minden szám ugyanabból az adatlap-rétegből jön, mint a feltárás
(build_profiles a jelölt mérési pillanatában), így a felület pontosan azt mutatja, amit a kereső látott:
- magyarázat: a mérés, a rossz-küszöb, a minta feltételei szavakban;
- "mi közös még": dimenziónként a rossz egységek leggyakoribb értéke, és ugyanennek az aránya a
  populációban (ha a rosszak 5/6-a ugyanabból a csoportból jön, az a valódi összekötő pont);
- egységek: a mintán belüli és kívüli tickek/szakaszok a mért értékkel és a mérést meghatározó eseményekkel;
- napi trend: a minta és a populáció rossz aránya naponta (a minta nyitás/érkezés napja szerint);
- eloszlás: a mért értékek sávonként a mintában és azon kívül.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from learning.mining.contrast_mining import with_implied
from learning.mining.measures import MEASURES_BY_KEY
from learning.mining.profiles import DIMENSIONS, DISPLAY_ONLY_DIMENSIONS, TIME_DIMENSIONS, build_profiles, dimension_values, has_value
from learning.state.candidate import Candidate


DIMENSION_LABELS = {
    "current_group": "jegyhez rendelt csoport (a mérés pillanatában)",
    "assignment_group": "csoport a mintavételkor", "business_service": "business service", "subcategory": "alkategória",
    "u_responsibility_unit": "felelős egység", "location": "helyszín", "impact": "hatás",
    "contact_type": "bejelentés módja", "date": "nap", "weekday": "hét napja", "hour": "óra",
    "time_basis": "a mérés alapja (munkaóra, ha a csoport munkaideje ismert)",
}

WEEKDAY_LABELS = {"Mon": "hétfő", "Tue": "kedd", "Wed": "szerda", "Thu": "csütörtök", "Fri": "péntek",
                  "Sat": "szombat", "Sun": "vasárnap"}

DURATION_BINS = (0, 1, 2, 4, 8, 16, 24, 48, 72, 168)

COUNT_BINS = (0, 1, 2, 3, 4, 5, 6, 8, 10)

def dimension_label(dim: str, scope: str) -> str:
    """A csoport kétféle értelme: a szakasznál akinél a mintavételkor volt, a ticketnél a jegy útja.

    Hívja: analysis.analyze_candidate, analysis.breakdown."""
    if dim == "assignment_group":
        return "csoport a mintavételkor (akinél a szakasz alatt volt)" if scope == "stint" else "a jegy útjába eső csoportok"
    return DIMENSION_LABELS.get(dim, dim)

def _value_text(dim: str, value: str) -> str:
    """Egy mezőérték olvasható alakja (a hét napja magyarul, az óra sávként).

    Hívja: DiscoveryService.about, analysis.analyze_candidate, analysis.breakdown, analysis.describe_condition."""
    if dim == "weekday":
        return WEEKDAY_LABELS.get(value, value)
    if dim == "hour":
        return f"{value}:00–{value}:59"
    return value

def describe_condition(dim: str, value: str, scope: str) -> str:
    """Egy mintafeltétel szavakban; az időbeli dimenzió ticketnél a nyitás, szakasznál az érkezés ideje.

    Hívja: DiscoveryService.about, analysis.analyze_candidate."""
    label = DIMENSION_LABELS.get(dim, dim)
    if dim in TIME_DIMENSIONS:
        when = "a csoporthoz érkezés" if scope == "stint" else "a ticket nyitásának"
        noun = {"weekday": "napja", "hour": "órája", "date": "dátuma"}[dim]
        return f"{when} {noun}: {_value_text(dim, value)}"
    if dim == "assignment_group" and scope == "ticket":
        return f"a ticket útjában szerepel a(z) {value} megoldócsoport"
    if scope == "stint" and dim == "assignment_group":
        return f"csoport a mintavételkor (akinél a szakasz alatt volt): {value}"
    return f"{label}: {value}" + (" (érkezéskor)" if scope == "stint" else "")

def breakdown(candidate: Any, profiles: Any, bad_threshold: float, window_start: datetime,
              per_dimension: int = 2, top: int = 15, min_difference: float = 0.1) -> Dict[str, Any]:
    """Tovább bontott mintához: mi jellemző a minta érintett eseteire a minta ÖSSZES esetéhez képest
    (pl. a szerdai lassú jegyek fele 1-es alkategória, holott a szerdai jegyeknek csak ötöde az).

    Leíró bontás, statisztikai szűrő nélkül: kis mintán is megmutatja, merre érdemes nézni. Mezőnként a
    leggyakoribb néhány értéket nézi, így a több részből álló magyarázat is látszik.

    Hívja: DiscoveryService._good_dimension_insights, DiscoveryService._refresh_breakdown."""
    measure = MEASURES_BY_KEY[candidate.measure]
    items = candidate.items
    units = [p for p in profiles.for_measure(measure)
             if p.measures.get(measure.key) is not None and p.opened_at >= window_start
             and all(has_value(p.dimensions, d, v) for d, v in items)]
    bad_units = [p for p in units if measure.is_bad(p.measures[measure.key], bad_threshold)]
    pattern_dims = {d for d, _ in items}
    rows = []
    if bad_units:
        dims = [d for d in DISPLAY_ONLY_DIMENSIONS + DIMENSIONS + TIME_DIMENSIONS if d not in pattern_dims]
        dims += sorted({d for p in units for d in p.dimensions if d not in dims and d not in pattern_dims})   # almérés-szempontok is
        dims = [d for d in dims if d != "time_basis"]   # a mérés alapja nem bontási szempont
        for dim in dims:
            counter = Counter(v for p in bad_units for v in dimension_values(p.dimensions, dim))
            for value, count in counter.most_common(per_dimension):
                share_bad = count / len(bad_units)
                share_all = sum(1 for p in units if has_value(p.dimensions, dim, value)) / len(units)
                if count >= 2 and share_bad - share_all >= min_difference:
                    rows.append({"dimension": dim, "label": dimension_label(dim, measure.scope), "value": value,
                                 "value_text": _value_text(dim, value), "bad_count": count, "bad_total": len(bad_units),
                                 "share_in_bad": share_bad, "share_in_pattern": share_all, "difference": share_bad - share_all})
    rows.sort(key=lambda r: (-r["difference"], -r["bad_count"]))
    return {"units": len(units), "bad": len(bad_units), "rows": rows[:top], "measured_at": window_start.isoformat()}

def brief(analysis: Mapping[str, Any], examples: int = 3) -> Dict[str, Any]:
    """Az elemzés tömör változata a jelöltsor kártyájára: a minta szavakban, az összefoglaló, az ítélet és
    a legkiugróbb érintett esetek indoklása (döntéskor ott legyen az elemző előtt).

    Hívja: DiscoveryService._brief_queue."""
    affected = sorted((u for u in analysis["units"] if u["in_pattern"] and u["bad"]), key=lambda u: -(u["value"] or 0))
    return {
        "conditions": analysis["conditions"],
        "summary": analysis["summary"],
        "verdict": analysis["verdict"]["text"],
        "common_points": [f"{c['label']}: {c['value_text']}" for c in analysis["verdict"]["common_points"]],
        "examples": [{"incident_id": u["incident_id"], "reason": u["reason"]} for u in affected[:examples]],
    }

def _bins(kind: str):
    """A mért érték hisztogramjának sávhatárai és címkéi (időtartamnál órában, darabszámnál darabban).

    Hívja: analysis.analyze_candidate."""
    edges = DURATION_BINS if kind == "duration" else COUNT_BINS
    labels = [f"{lo}–{hi}" for lo, hi in zip(edges, edges[1:])] + [f"{edges[-1]}+"]
    return edges, labels

def _bin_index(edges, value: float) -> int:
    """Melyik hisztogram-sávba esik egy érték.

    Hívja: analysis.analyze_candidate."""
    for index in range(len(edges) - 1):
        if value < edges[index + 1]:
            return index
    return len(edges) - 1

def analyze_candidate(
    candidate: Candidate,
    incidents: Sequence[Any],
    as_of: datetime,
    lookback_days: float,
    bad_threshold: float,
    levels: Optional[Mapping[str, str]] = None,
    trend_days: int = 56,
    span_profiles: Optional[Any] = None,
    derived: Sequence[Tuple[str, str]] = (),
    schedules: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """`span_profiles`: előre felépített adatlapok (ProfileSet) ugyanarra a pillanatra és időszakra – sok jelölt
    egyszerre elemzésekor (pl. a napi jelöltsor összefoglalóinál) így csak egyszer épülnek fel.

    Hívja: DiscoveryService._brief_queue, DiscoveryService.analysis."""
    measure = MEASURES_BY_KEY[candidate.measure]
    window_start = as_of - timedelta(days=lookback_days)
    items = candidate.items

    # A trendhez egy hosszabb időszak egységei (a mérési pillanatban látott értékekkel)
    if span_profiles is None:
        span_profiles = build_profiles(incidents, as_of, as_of - timedelta(days=max(trend_days, lookback_days)), levels, derived, schedules)
    span = span_profiles.for_measure(measure)
    measured = [p for p in span if p.measures.get(measure.key) is not None]
    window = [p for p in measured if p.opened_at >= window_start]

    def matches(profile) -> bool:
        return all(has_value(profile.dimensions, dim, value) for dim, value in items)

    def bad(profile) -> bool:
        return measure.is_bad(profile.measures[measure.key], bad_threshold)

    in_pattern = [p for p in window if matches(p)]
    rest = [p for p in window if not matches(p)]
    bad_in = [p for p in in_pattern if bad(p)]
    population_bad = sum(1 for p in window if bad(p))

    # Mi közös még a rossz egységekben (a mintán kívüli dimenziók szerint)
    pattern_dims = {dim for dim, _ in items}
    common = []
    for dim in DISPLAY_ONLY_DIMENSIONS + DIMENSIONS + TIME_DIMENSIONS:
        if dim in pattern_dims or not bad_in:
            continue
        counter = Counter(value for p in bad_in for value in dimension_values(p.dimensions, dim))
        if not counter:
            continue
        value, count = counter.most_common(1)[0]
        in_population = sum(1 for p in window if has_value(p.dimensions, dim, value))
        common.append({
            "dimension": dim, "label": dimension_label(dim, measure.scope), "value": value,
            "value_text": _value_text(dim, value), "bad_count": count, "bad_total": len(bad_in),
            "share_in_bad": count / len(bad_in), "share_in_population": in_population / len(window) if window else 0.0,
            "distinct_values": len(counter),
        })
    common.sort(key=lambda c: (-(c["share_in_bad"] - c["share_in_population"]), c["label"]))
    for row in common:
        row["difference"] = row["share_in_bad"] - row["share_in_population"]
        # közös pont: az érintettek nagy része osztozik rajta, és a populációban jóval ritkább
        row["strong"] = row["share_in_bad"] >= 0.8 and row["difference"] >= 0.3
        row["notable"] = row["difference"] >= 0.15

    # Egységek: a mintán belül és kívül, a mérést meghatározó eseményekkel
    def unit(profile, in_pat: bool) -> Dict[str, Any]:
        evidence = profile.evidence.get(measure.key, ())
        # a felvételi mérésnél: ha a mérés pillanatáig nem volt felelős, az érték az eddigi várakozás
        pending = bool(measure.pending_reason) and not any(e.get("field") == "assigned_to" for e in evidence)
        value = profile.measures[measure.key]
        return {
            "incident_id": profile.incident_id, "unit_id": profile.unit, "start": profile.opened_at.isoformat(),
            "value": value, "bad": bad(profile), "in_pattern": in_pat, "pending": pending,
            "reason": measure.explain(value, bad_threshold, pending),
            "dimensions": {dim: ", ".join(dimension_values(profile.dimensions, dim))
                           for dim in DISPLAY_ONLY_DIMENSIONS + DIMENSIONS + TIME_DIMENSIONS},
            "evidence": [{"timestamp": str(e.get("timestamp", "")), "field": e.get("field"), "old_value": e.get("old_value"),
                          "new_value": e.get("new_value")} for e in profile.evidence.get(measure.key, ())][:6],
        }

    # Napi trend: a minta és a populáció rossz aránya a kezdés napja szerint
    daily: Dict[str, Dict[str, int]] = defaultdict(lambda: {"pattern": 0, "pattern_bad": 0, "all": 0, "all_bad": 0})
    for profile in measured:
        day = profile.opened_at.date().isoformat()
        row = daily[day]
        row["all"] += 1
        row["all_bad"] += bad(profile)
        if matches(profile):
            row["pattern"] += 1
            row["pattern_bad"] += bad(profile)
    # a trend az adat első napjától indul (rövid adatnál ne rajzoljon üres heteket)
    earliest = min((p.opened_at.date() for p in measured), default=as_of.date())
    first_day = max((as_of - timedelta(days=trend_days)).date(), earliest)
    trend = []
    for offset in range((as_of.date() - first_day).days + 1):
        day = (first_day + timedelta(days=offset)).isoformat()
        row = daily.get(day, {"pattern": 0, "pattern_bad": 0, "all": 0, "all_bad": 0})
        trend.append({"day": day, **row,
                      "pattern_rate": row["pattern_bad"] / row["pattern"] if row["pattern"] else None,
                      "all_rate": row["all_bad"] / row["all"] if row["all"] else None,
                      "in_window": day >= window_start.date().isoformat()})

    # Eloszlás: a mért értékek sávonként (az egységek arányában), mintán belül és kívül
    edges, labels = _bins(measure.kind)
    histogram = [{"bin": label, "pattern": 0, "rest": 0} for label in labels]
    for profile in in_pattern:
        histogram[_bin_index(edges, profile.measures[measure.key])]["pattern"] += 1
    for profile in rest:
        histogram[_bin_index(edges, profile.measures[measure.key])]["rest"] += 1
    for row in histogram:
        row["pattern_share"] = row["pattern"] / len(in_pattern) if in_pattern else 0.0
        row["rest_share"] = row["rest"] / len(rest) if rest else 0.0

    # Ítélet: köti-e valami (a minta feltételén túl) össze az érintetteket, és mennyi volt még folyamatban
    strong = [row for row in common if row["strong"]]
    pending_bad = sum(1 for p in bad_in if unit(p, True)["pending"])
    verdict = {
        "common_points": [{"label": r["label"], "value": r["value"], "value_text": r["value_text"],
                           "share_in_bad": r["share_in_bad"], "share_in_population": r["share_in_population"]} for r in strong],
        "pending_share": pending_bad / len(bad_in) if bad_in else 0.0,
        "pending_count": pending_bad,
    }
    if strong:
        verdict["text"] = "Közös pont: " + "; ".join(
            f"{r['label']} {r['value_text']} ({r['bad_count']}/{r['bad_total']} érintett, a populációban {r['share_in_population']:.0%})"
            for r in strong)
    elif bad_in:
        verdict["text"] = ("Nincs közös pont: a minta feltételén kívül semmi nem köti össze az érintett eseteket – "
                           "egyik dimenzióban sem hasonlítanak jobban egymásra, mint a teljes populáció. "
                           "Nem egy csoport vagy service problémája.")
    else:
        verdict["text"] = "Nincs érintett eset a mintában."
    if bad_in and verdict["pending_share"] >= 0.5 and measure.pending_reason:
        verdict["text"] += (f" Az érintettek {verdict['pending_share']:.0%}-a ({pending_bad} eset) a mérés pillanatában még várt: "
                            "a kiugrást valószínűleg a munkaidőn kívüli (éjszakai, hétvégi) várakozás okozza.")

    scope_word = "szakasz" if measure.scope == "stint" else "ticket"
    conditions = [describe_condition(dim, value, measure.scope) for dim, value in items]
    rate = len(bad_in) / len(in_pattern) if in_pattern else 0.0
    population_rate = population_bad / len(window) if window else 0.0
    summary = (f"{len(in_pattern)} {scope_word} illik a mintára az ablakban, ebből {len(bad_in)} érintett ({rate:.0%}); "
               f"a teljes populációban ({len(window)} {scope_word}) az érintettek aránya {population_rate:.1%}.")
    if candidate.kind == "trend":
        history = candidate.stats.get("history_rate")
        summary += (f" Ez a saját múltjához mért romlás: a megelőző időszakban ugyanitt {history:.0%} volt."
                    if history is not None else "")
    # a nap, amelyre a "nem dolgozik" tény vonatkozhatna: a mintából, vagy ha minden rossz eset azonos napú
    weekday = dict(with_implied(tuple(sorted(items)))).get("weekday")
    if weekday is None and bad_in:
        days = {p.opened_at.weekday() for p in bad_in}
        weekday = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[days.pop()] if len(days) == 1 else None
    return {
        "candidate_id": candidate.candidate_id, "kind": candidate.kind, "state": candidate.state,
        "weekday": weekday, "weekday_text": WEEKDAY_LABELS.get(weekday, "") if weekday else "",
        "time_sensitive": measure.time_sensitive,
        "measure": {"key": measure.key, "label": measure.label, "description": measure.description,
                    "unit": measure.unit, "kind": measure.kind, "scope": measure.scope, "threshold": bad_threshold},
        "conditions": conditions, "items": [list(item) for item in items], "alternatives": candidate.alternatives,
        "window": {"start": window_start.isoformat(), "end": as_of.isoformat()},
        "summary": summary,
        "stats": {"pattern": len(in_pattern), "pattern_bad": len(bad_in), "pattern_rate": rate,
                  "population": len(window), "population_bad": population_bad, "population_rate": population_rate,
                  "lift": candidate.stats.get("lift"), "q_value": candidate.stats.get("q_value")},
        "common": common,
        "verdict": verdict,
        "units": [unit(p, True) for p in in_pattern] + [unit(p, False) for p in rest],
        "trend": trend,
        "histogram": histogram,
    }
