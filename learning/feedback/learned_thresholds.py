"""Tanult küszöb-javaslatok a gyári detektorokhoz: ahol a túllépés a szokásos, ott az nem hiba.

Egy jegyszintű detektor (pl. ping-pong: csoportváltások száma) globális küszöbe mindenhol ugyanaz. Ha egy entitásnál
(service, csoport, alkategória) a jegyek többsége túllépi, akkor ott a túllépés a szokásos működés (pl. egy több
csapatot érintő service-nél a sok átadás), és a detektor ott csak zajt termel. A javaslat: az ottani értékek 90.
percentilise legyen a mérsékelt küszöb (így a saját normájához képest kiugró jegy továbbra is jelez), az extrém
küszöb ugyanannyival feljebb, mint a globálisnál.

Elfogadva „tanult” szűkebb küszöb lesz (models.diagnostics.core.threshold_overrides); a kézzel adott küszöb nyer
felette. Ez a modul tiszta: a megfigyelésekből számol, a végrehajtás a szolgáltatásé.
"""

import math
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


LEARN_DIMENSIONS = {"business_service": "service", "assignment_group": "csoport (a jegy útjában)", "subcategory": "alkategória"}

def _percentile(values: Sequence[float], share: float) -> float:
    """Egy értéklista adott percentilise (a legközelebbi rang szerint).

    Hívja: learned_thresholds.suggestions."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(math.ceil(share * len(ordered))) - 1)] if ordered else 0.0

def suggestions(diagnostic_key: str, label: str, base: Mapping[str, Any], rows: Iterable[Tuple[Any, ...]],
                existing_scopes: Iterable[Tuple[Tuple[str, str], ...]] = (), dismissed: Mapping[str, str] = None,
                min_cases: int = 8, min_share: float = 0.5, min_span_days: int = 14, limit: int = 10) -> List[Dict[str, Any]]:
    """rows: megfigyelésenként (leírás [(mező, érték)], mért érték[, a jegy nyitása (datetime)]). A mérsékelt / extrém
    küszöb a `base` "moderate_*" és "extreme_*" kulcsa. Csak ott javasol, ahol legalább `min_cases` jegy van, legalább
    `min_share` részük túllép, és (ha ismert a nyitás ideje) a túllépés tartós: az első és az utolsó túllépő jegy között
    legalább `min_span_days` nap telt el – egy friss hiba ne látsszon „szokásosnak”.

    Hívja: DiscoveryService._threshold_suggestions."""
    moderate = next((k for k in base if k.startswith("moderate")), None)
    extreme = next((k for k in base if k.startswith("extreme")), None)
    if moderate is None or extreme is None:
        return []
    dismissed = dismissed or {}
    taken = {tuple(sorted(scope)) for scope in existing_scopes}
    groups: Dict[Tuple[str, str], List[float]] = {}
    moments: Dict[Tuple[str, str], List[Any]] = {}
    for row in rows:
        items, value = row[0], float(row[1] or 0)
        opened = row[2] if len(row) > 2 else None
        for dim, val in dict.fromkeys((d, v) for d, v in items if d in LEARN_DIMENSIONS and v):
            groups.setdefault((dim, str(val)), []).append(value)
            if opened is not None and value >= base[moderate]:
                moments.setdefault((dim, str(val)), []).append(opened)
    result = []
    for (dim, val), values in groups.items():
        n = len(values)
        exceeding = sum(1 for v in values if v >= base[moderate])
        if n < min_cases or exceeding / n < min_share or ((dim, val),) in taken:
            continue
        when = moments.get((dim, val))
        if when and (max(when) - min(when)).days < min_span_days:
            continue   # friss: lehet, hogy hiba, nem a szokásos működés
        new_moderate = max(int(base[moderate]) + 1, int(math.ceil(_percentile(values, 0.9))))
        new_extreme = max(int(base[extreme]), new_moderate + int(base[extreme]) - int(base[moderate]))
        key = f"{diagnostic_key}|{dim}={val}"
        if dismissed.get(key) == str(new_moderate):
            continue
        result.append({
            "key": key, "diagnostic": diagnostic_key, "label": label, "scope": [[dim, val]], "dimension_label": LEARN_DIMENSIONS[dim],
            "params": {moderate: new_moderate, extreme: new_extreme}, "base": {moderate: base[moderate], extreme: base[extreme]},
            "cases": n, "exceeding": exceeding, "share": round(exceeding / n, 3), "p90": _percentile(values, 0.9),
            "message": (f"{label}: a(z) {val} {LEARN_DIMENSIONS[dim]} {n} jegyéből {exceeding} ({exceeding / n:.0%}) túllépi a globális "
                        f"{base[moderate]:g}-es küszöböt – itt ez a szokásos. Javaslat: {new_moderate} (extrém {new_extreme}); így a "
                        f"saját normájához képest kiugró jegy továbbra is jelez."),
        })
    result.sort(key=lambda s: (-s["share"], -s["cases"]))
    return result[:limit]
