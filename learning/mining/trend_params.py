from dataclasses import dataclass
from typing import Tuple


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


# Entitás-dimenziók: ezekre van értelme a "saját múlt"-nak (az időbeli dimenzióknak nincs)
