"""Ismert eltérés: egy minta szokásos eltérése a mindenkori alaparánytól.

Egy tartósan kilógó csoportot (pl. alacsony rendelkezésre állású csapat) nem a globális
alaparányhoz, hanem a saját szokásos eltéréséhez mérünk:

    elvárt arány = min(1, alaparány most × szokásos lift)

A szokásos lift a minta futásonként rögzített darabszámaiból, összesítve számolódik
(nem heti liftek átlagaként), egymást nem átfedő ablakokon, és a kevés adat 1,0 felé húzódik.
Ha az egész rendszer romlik, az alaparánnyal az elvárás is nő; csak a minta saját romlása jelez.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence

from learning.mining.contrast_mining import binomial_sf
from learning.mining.profiles import has_value


def pattern_counts(profiles, measure, items, bad_threshold: Optional[float] = None) -> Dict[str, int]:
    """Egy minta darabszámai az adatlapokon: rossz és összes a mintában és a teljes populációban.

    Hívja: runner._counts."""
    population = [p for p in profiles if p.measures.get(measure.key) is not None]
    bad_flags = [measure.is_bad(p.measures[measure.key], bad_threshold) for p in population]
    in_group = [all(has_value(p.dimensions, dim, value) for dim, value in items) for p in population]
    return {
        "bad": sum(1 for bad, member in zip(bad_flags, in_group) if bad and member),
        "size": sum(in_group),
        "population_bad": sum(bad_flags),
        "population": len(population),
    }

def non_overlapping(counts: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """A legfrissebbtől visszafelé egymást nem átfedő ablakok (napi futásnál a 7 napos ablakok
    átfednének, és ugyanaz a ticket többször számítana).

    Hívja: DiscoveryManager.baseline_progress, baseline.freeze_baseline."""
    chosen: List[Mapping[str, Any]] = []
    for record in sorted(counts, key=lambda r: r["window_end"], reverse=True):
        if not chosen or record["window_end"] <= chosen[-1]["window_start"]:
            chosen.append(record)
    return chosen

def freeze_baseline(
    counts: Sequence[Mapping[str, Any]], frozen_at: str, min_size: int, prior: float
) -> Optional[Dict[str, Any]]:
    """A szokásos lift a rögzített darabszámokból; None, ha a mintában kevés a ticket.

    Simítás: a minta aránya `prior` darab "átlagos" tickettel kiegészítve, így kis mintán a lift
    1,0 felé húzódik: (rossz + prior × alaparány) / (méret + prior).
    

    Hívja: DiscoveryManager.decide, DiscoveryManager.ingest."""
    windows = non_overlapping(counts)
    totals = {key: sum(r[key] for r in windows) for key in ("bad", "size", "population_bad", "population")}
    if totals["size"] < min_size or not totals["population"] or not totals["population_bad"]:
        return None
    base_rate = totals["population_bad"] / totals["population"]
    smoothed_rate = (totals["bad"] + prior * base_rate) / (totals["size"] + prior)
    return {
        "lift": smoothed_rate / base_rate,
        "raw_lift": (totals["bad"] / totals["size"]) / base_rate,
        **totals,
        "windows": len(windows),
        "frozen_at": frozen_at,
    }

def compare_to_baseline(record: Mapping[str, Any], usual_lift: float) -> Optional[Dict[str, Any]]:
    """Egy ablak darabszámai a szokásos eltéréshez mérve; None, ha nincs mit mérni.

    Hívja: DiscoveryManager._exceeds_baseline."""
    if not record["size"] or not record["population"]:
        return None
    base_rate = record["population_bad"] / record["population"]
    expected_rate = min(1.0, base_rate * usual_lift)
    rate = record["bad"] / record["size"]
    return {
        "rate": rate,
        "base_rate": base_rate,
        "expected_rate": expected_rate,
        "ratio": rate / expected_rate if expected_rate else 0.0,
        "p_value": binomial_sf(record["bad"], record["size"], expected_rate) if expected_rate else 1.0,
    }
