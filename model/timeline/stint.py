from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from model.timeline.field_change import FieldChange
from model.timeline.fields import hours_between
from model.timeline.interval import Interval


@dataclass(frozen=True)
class Stint:
    """Egy csoportnál töltött szakasz: [start, end), az `as_of`-ig még tartó szakasznál end = as_of."""

    index: int
    group: str
    start: datetime
    end: datetime
    end_reason: str                     # transfer | closed | open
    next_group: str                     # továbbításnál a következő csoport
    changes: Tuple[FieldChange, ...]    # a szakasz alatti változások (a kezdő csoportváltással együtt)
    holds: Tuple[Interval, ...]         # a szakaszra vágott hold-időszakok
    # Az érkezés mentésének minden változása, a naplóbeli sorrendtől függetlenül (azonos időbélyegű
    # sorok sorrendje esetleges: a felelős törlése vagy beállítása a csoportváltás előtt is állhat)
    arrival: Tuple[FieldChange, ...] = ()

    @property
    def is_open(self) -> bool:
        return self.end_reason == "open"

    @property
    def hours(self) -> float:
        return hours_between(self.start, self.end)

    @property
    def hold_hours(self) -> float:
        return sum(interval.hours for interval in self.holds)

    @property
    def first_assignment(self) -> Optional[FieldChange]:
        """Az első felelőshöz rendelés a szakaszban (a csoporton belüli felvétel).

        A továbbítás mentésében beállított felelős a fogadó csoporté (a küldő a fogadó egy tagjára tette):
        így az érkezés mentése ide számít, a saját továbbító mentése viszont nem."""
        pool = list(self.arrival) + [c for c in self.changes if c.at > self.start]
        transfer = self.end_reason == "transfer"
        return next((c for c in pool if c.field == "assigned_to" and c.new and not (transfer and c.at >= self.end)), None)

    def changes_of(self, field: str) -> List[FieldChange]:
        return [c for c in self.changes if c.field == field]
