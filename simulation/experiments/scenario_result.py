from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional


@dataclass
class ScenarioResult:
    name: str
    kind: str
    group: str
    start: datetime
    end: datetime
    applied: int
    first_detected: Optional[datetime] = None
    days_in_fault: int = 0          # mért napok a hiba alatt
    days_flagged: int = 0           # ebből a hibának tulajdonítható jelzés
    natural_days: int = 0           # a tiszta futás is jelzett ugyanott (a hiba alatt)
    natural_before: int = 0         # jelzés a hiba előtt (hibás futás; a tisztával egyezik)
    flagged_groups: Dict[str, int] = field(default_factory=dict)   # mindenkire vonatkozó hibánál

    @property
    def delay_days(self) -> Optional[float]:
        return (self.first_detected - self.start).total_seconds() / 86400 if self.first_detected else None

    @property
    def stability(self) -> float:
        return self.days_flagged / self.days_in_fault if self.days_in_fault else 0.0
