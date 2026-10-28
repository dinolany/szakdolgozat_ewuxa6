from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Tuple, Union


DimensionValue = Union[str, Tuple[str, ...]]


@dataclass(frozen=True)
class TicketProfile:
    """Egy mérési egység (ticket vagy szakasz) adatlapja egy adott időpontban: dimenziói és mért értékei."""

    incident_id: str
    opened_at: datetime                 # ticketnél a nyitás, szakasznál a csoporthoz érkezés ideje
    dimensions: Mapping[str, DimensionValue]
    measures: Mapping[str, Optional[float]]
    # Mérésenként az az esemény (vagy események), amely a mért értéket meghatározta
    evidence: Mapping[str, Tuple[Dict[str, Any], ...]] = field(default_factory=dict)
    unit_id: str = ""                   # szakasznál "<incidens>#<sorszám>"; ticketnél üres

    @property
    def unit(self) -> str:
        """Az adatlap egysége az almérésekhez: (incidens, szakasz sorszáma vagy None)."""
        return self.unit_id or self.incident_id
