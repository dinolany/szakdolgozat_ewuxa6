from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Tuple


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
