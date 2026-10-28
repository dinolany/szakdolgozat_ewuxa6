from dataclasses import dataclass, field
from typing import List

from model.diagnostics.core import DiagnosticRun


@dataclass
class RegistryResult:
    runs: List[DiagnosticRun] = field(default_factory=list)
    changes: List[dict] = field(default_factory=list)

    @property
    def measurement_count(self) -> int:
        return sum(len(run.measurements) for run in self.runs)
