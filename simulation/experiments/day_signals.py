from dataclasses import dataclass
from typing import Dict, Set


@dataclass
class DaySignals:
    candidates: int
    hits: Dict[str, bool]
    groups: Dict[str, Set[str]]     # mindenkire vonatkozó forgatókönyvnél: a jelző csoportok
    unrelated: int
    decisions: int                  # elemzői döntés a jelöltekhez, ha a csomópontokat egyben döntjük el
