from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from model.timeline.fields import hours_between


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    open: bool = False  # az `as_of` pillanatában még tart

    @property
    def hours(self) -> float:
        return hours_between(self.start, self.end)

    def clip(self, start: datetime, end: datetime) -> Optional["Interval"]:
        lo, hi = max(self.start, start), min(self.end, end)
        return Interval(lo, hi, self.open and hi == self.end) if lo < hi else None
