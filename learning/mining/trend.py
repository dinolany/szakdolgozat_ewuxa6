"""Romlás a saját múlthoz képest: entitásonként (csoport, service, ...) a mostani ablak a saját előző heteihez.

A kontrasztkereső (mining.mine) minden csoportot a többihez méri ugyanabban az időszakban; egy lassan romló
csoport így sokáig "átlagosnak" látszik egy zajos populációban. Ez a kereső a csoportot önmagához méri:

    szokásos eltérés = a csoport aránya a múltban / a populáció aránya a múltban
    elvárt arány most = min(1, a populáció aránya most × szokásos eltérés)

és egyoldali binomiális próbával (FDR-korrekcióval, mérésenként) jelez, ha a mostani arány ennél
szignifikánsan és érdemben magasabb. A populációval együtt mozgó változás (rendszerszintű romlás) így nem
jelez, csak a csoport saját romlása. A múltból kimaradnak az elfogadott findingok rossz egységei (ismert
probléma), különben a romlott időszak lenne a "normális".
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from learning.mining.contrast_mining import PatternResult, benjamini_hochberg, binomial_sf
from learning.mining.measure_def import MeasureDef
from learning.mining.profiles import dimension_values
from learning.mining.ticket_profile import TicketProfile


# Entitás-dimenziók: ezekre van értelme a "saját múlt"-nak (az időbeli dimenzióknak nincs)
TREND_DIMENSIONS = ("assignment_group", "business_service", "subcategory", "u_responsibility_unit", "location")


@dataclass(frozen=True)
class TrendParams:
    history_weeks: float = 6       # a múlt hossza (a mostani ablak előtt)
    min_history: int = 20          # legalább ennyi mért egység kell a múltban
    min_ratio: float = 1.5         # a mostani arány legalább ennyiszerese az elvártnak
    prior: float = 10.0            # simítás: ennyi "átlagos" egység húzza a szokásos eltérést 1 felé
    fdr_q: float = 0.01
    min_support: int = 3
    dimensions: Tuple[str, ...] = TREND_DIMENSIONS   # csak a teljes szerepű entitás-dimenziók


TREND_KIND = "trend"

def _counts(profiles: Sequence[TicketProfile], measure: MeasureDef, bad_threshold: Optional[float],
            exclude: Set[str] = frozenset(), dimensions: Sequence[str] = TREND_DIMENSIONS):
    """(dimenzió, érték) -> [rossz, összes], a populáció rossz/összes, és a rossz egységek incidensei.

    Hívja: trend.mine_trends."""
    per_item: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0])
    bad_ids: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    total_bad = total = 0
    for profile in profiles:
        value = profile.measures.get(measure.key)
        if value is None or profile.incident_id in exclude:
            continue
        bad = measure.is_bad(value, bad_threshold)
        total += 1
        total_bad += bad
        for dim in dimensions:
            for item_value in dimension_values(profile.dimensions, dim):
                counts = per_item[(dim, item_value)]
                counts[0] += bad
                counts[1] += 1
                if bad:
                    bad_ids[(dim, item_value)].add(profile.incident_id)
    return per_item, total_bad, total, bad_ids

def mine_trends(
    current: Sequence[TicketProfile],
    history: Sequence[TicketProfile],
    measure: MeasureDef,
    params: TrendParams = TrendParams(),
    bad_threshold: Optional[float] = None,
    exclude_history: Iterable[str] = (),
) -> List[PatternResult]:
    """Romlás-keresés: minták, amelyek a saját múltjukhoz képest romlottak (a populáció változásával korrigálva).

    Hívja: runner._mine_trends."""
    now_items, now_bad, now_total, now_bad_ids = _counts(current, measure, bad_threshold, set(), params.dimensions)
    past_items, past_bad, past_total, _ = _counts(history, measure, bad_threshold, set(exclude_history), params.dimensions)
    if not now_total or not past_total or not past_bad or not now_bad:
        return []
    now_rate, past_rate = now_bad / now_total, past_bad / past_total

    tested = []
    for item, (bad, size) in now_items.items():
        past_bad_item, past_size = past_items.get(item, (0, 0))
        if bad < params.min_support or past_size < params.min_history:
            continue
        usual = ((past_bad_item + params.prior * past_rate) / (past_size + params.prior)) / past_rate
        expected = min(1.0, now_rate * usual)
        tested.append((item, bad, size, past_bad_item, past_size, usual, expected,
                       binomial_sf(bad, size, expected) if expected < 1 else 1.0))
    if not tested:
        return []
    q_values = benjamini_hochberg([entry[-1] for entry in tested])

    results = []
    for (item, bad, size, past_bad_item, past_size, usual, expected, p_value), q_value in zip(tested, q_values):
        rate = bad / size
        if q_value > params.fdr_q or not expected or rate / expected < params.min_ratio:
            continue
        results.append(PatternResult(
            measure=measure.key, items=(item,), bad_count=bad, group_size=size, population=now_total,
            base_rate=expected, rate=rate, lift=rate / expected, expected=size * expected,
            excess=bad - size * expected, p_value=p_value, q_value=q_value, distinct_days=0,
            bad_incident_ids=tuple(sorted(now_bad_ids[item])), kind=TREND_KIND,
            extra={"history_rate": past_bad_item / past_size, "history_size": past_size,
                   "usual_lift": usual, "population_rate_now": now_rate, "population_rate_history": past_rate},
        ))
    results.sort(key=lambda r: (r.p_value, -r.excess, r.key))
    return results
