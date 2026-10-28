from dataclasses import dataclass, field
from typing import List


@dataclass
class IngestReport:
    measured_at: str
    new: List[str] = field(default_factory=list)
    updated: List[str] = field(default_factory=list)
    filtered: List[str] = field(default_factory=list)
    returned: List[str] = field(default_factory=list)
    exceeds_baseline: List[str] = field(default_factory=list)
