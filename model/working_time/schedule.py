"""Munkaidő: csoportonként mikor dolgoznak, és az eltelt idő munkaórában.

A felvételi és a hold idő falióra szerint félrevezető: a pénteken délután érkező, hétfő reggel felvett jegy
65 óra, holott a csoport 1 munkaórán belül reagált. Ahol a csoport munkaideje ismert, a mérés munkaórában
számol; ahol nem, marad a falióra.

A munkaidő forrása (a mérés a legjobbat használja):
- tanult: a csoport tényleges aktivitásából javasolt és az elemző által jóváhagyott munkaidő-tény;
- kézi: az elemző által rögzített munkaidő-tény;
- átvett: később a ServiceNow munkarendjeiből (cmn_schedule) – ugyanaz a tényfajta, más forrással.

A javaslat ellenőrizhető a ServiceNow saját munkaidő-mezőjével (business_stc: a lezárásig eltelt munkaidő
másodpercben, a jegy munkarendje szerint).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple


WEEKDAY_CODES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

WEEKDAY_LABELS = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")

@dataclass(frozen=True)
class Schedule:
    days: FrozenSet[int]      # 0 = hétfő
    start: int                # óra (0-23)
    end: int                  # óra (1-24), kizárólagos; start >= end: éjszakán átnyúló műszak
    source: str = "kézi"

    @property
    def continuous(self) -> bool:
        return len(self.days) == 7 and self.start == 0 and self.end == 24

    def label(self) -> str:
        if self.continuous:
            return "folyamatos (0–24, minden nap)"
        return f"{days_label(self.days)} {self.start}–{self.end}"

    def _intervals(self, day: datetime) -> List[Tuple[datetime, datetime]]:
        """Az adott naptári napon kezdődő munkaidő-szakasz(ok)."""
        if day.weekday() not in self.days:
            return []
        begin = day.replace(hour=0, minute=0, second=0, microsecond=0)
        if self.start < self.end:
            return [(begin + timedelta(hours=self.start), begin + timedelta(hours=self.end))]
        return [(begin + timedelta(hours=self.start), begin + timedelta(days=1, hours=self.end))]   # éjszakai műszak

    def working_seconds(self, start: datetime, end: datetime) -> float:
        if end <= start:
            return 0.0
        if self.continuous:
            return (end - start).total_seconds()
        total = 0.0
        day = (start - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= end:
            for a, b in self._intervals(day):
                lo, hi = max(a, start), min(b, end)
                if hi > lo:
                    total += (hi - lo).total_seconds()
            day += timedelta(days=1)
        return total

    def working_hours(self, start: datetime, end: datetime) -> float:
        return self.working_seconds(start, end) / 3600

def days_label(days: Iterable[int]) -> str:
    days = sorted(set(days))
    if days == list(range(5)):
        return "hétfő–péntek"
    if days == list(range(7)):
        return "minden nap"
    if days and days == list(range(days[0], days[-1] + 1)) and len(days) > 2:
        return f"{WEEKDAY_LABELS[days[0]]}–{WEEKDAY_LABELS[days[-1]]}"
    return ", ".join(WEEKDAY_LABELS[d] for d in days)

def parse_days(text: str) -> FrozenSet[int]:
    text = str(text or "").strip()
    if text in ("*", "minden"):
        return frozenset(range(7))
    return frozenset(WEEKDAY_CODES.index(part.strip()) for part in text.split(",") if part.strip() in WEEKDAY_CODES)

def schedule_from_fact(args: Mapping[str, str], source: str = "kézi") -> Optional[Schedule]:
    """munkaido(csoport, napok, tol, ig): napok "Mon,Tue,…" vagy "*"; tol / ig egész óra (ig 1–24)."""
    try:
        days = parse_days(args.get("napok", ""))
        start, end = int(str(args.get("tol", "")).split(":")[0]), int(str(args.get("ig", "")).split(":")[0])
    except ValueError:
        return None
    if not days or not (0 <= start <= 23) or not (1 <= end <= 24):
        return None
    return Schedule(days, start, end, source)

def fact_args(group: str, schedule: Schedule) -> Dict[str, str]:
    return {"csoport": group, "napok": "*" if len(schedule.days) == 7 else ",".join(WEEKDAY_CODES[d] for d in sorted(schedule.days)),
            "tol": str(schedule.start), "ig": str(schedule.end)}
