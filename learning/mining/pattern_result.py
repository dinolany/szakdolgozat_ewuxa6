from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

from learning.mining.pattern import Pattern, pattern_key


@dataclass(frozen=True)
class PatternResult:
    measure: str
    items: Pattern
    bad_count: int           # rossz egységek (ticketek vagy szakaszok) száma
    group_size: int
    population: int
    base_rate: float
    rate: float
    lift: float
    expected: float          # várható rossz ticketszám alaparány mellett
    excess: float            # többlet rossz ticket a várthoz képest
    p_value: float
    q_value: float           # FDR-korrigált p-érték
    distinct_days: int       # hány különböző napon fordult elő rossz ticket a mintában
    bad_incident_ids: Tuple[str, ...]
    # Más minták, amelyek pontosan ugyanezt a rossz ticket-halmazt írják le (összevonva)
    alternatives: Tuple[Pattern, ...] = ()
    # contrast: a többi egységhez képest kiugró (mine); trend: a saját múltjához képest romlott (trend.py)
    kind: str = "contrast"
    extra: Optional[Dict[str, Any]] = None   # fajtától függő többletadat (pl. a múltbeli arány, a kontextus)

    @property
    def key(self) -> str:
        """A minta kulcsa (mező=érték&…).

        Hívja: PatternResult.candidate_key (átadva)."""
        return pattern_key(self.items)

    @property
    def candidate_key(self) -> str:
        """A jelölt azonosításának kulcsa: ugyanaz a minta más fajtaként külön jelölt."""
        return self.key if self.kind == "contrast" else f"{self.kind}|{self.key}"

    @property
    def alternative_keys(self) -> Tuple[str, ...]:
        """Az ugyanazt az esethalmazt leíró további minták kulcsai."""
        return tuple(pattern_key(items) for items in self.alternatives)

    @property
    def dimensions(self) -> Set[str]:
        """A minta mezői."""
        return {dimension for dimension, _ in self.items}
