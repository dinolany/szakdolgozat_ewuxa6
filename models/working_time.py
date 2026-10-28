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

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

WEEKDAY_CODES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_LABELS = ("hétfő", "kedd", "szerda", "csütörtök", "péntek", "szombat", "vasárnap")

# Nem emberi műveletek (rendszer írja, vagy a lezáró mentés kísérői): az aktivitásba nem számítanak
SYSTEM_FIELDS = frozenset({"email", "u_reaction_time", "reassignment_count", "u_old_assignment_group", "business_duration",
                           "business_stc", "calendar_duration", "calendar_stc", "resolved_at", "resolved_by",
                           "u_strike_counter", "active"})


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


# --------------------------------------------------------------------------
# Tanulás az aktivitásból
# --------------------------------------------------------------------------

def activity(timelines: Iterable, since: datetime, until: datetime) -> Dict[str, Counter]:
    """Csoportonként a saját mentések (nem rendszer-műveletek) száma hét napja × óra szerint."""
    result: Dict[str, Counter] = defaultdict(Counter)
    for timeline in timelines:
        for stint in timeline.stints:
            saves = {c.at for c in timeline.own_changes(stint) if c.field not in SYSTEM_FIELDS and since <= c.at <= until}
            for at in saves:
                result[stint.group][(at.weekday(), at.hour)] += 1
    return result


@dataclass(frozen=True)
class ScheduleSuggestion:
    group: str
    schedule: Schedule
    actions: int
    coverage: float           # az aktivitás ekkora része esik a javasolt munkaidőbe
    weekend_share: float


def infer_schedule(counts: Counter, min_actions: int = 50, day_share: float = 0.04, coverage: float = 0.75) -> Optional[ScheduleSuggestion]:
    """A legszűkebb munkaidő, amely az aktivitás `coverage` részét lefedi: munkanap az, amelyre legalább
    `day_share` jut; az órák a munkanapokon a legrövidebb, `coverage` részt lefedő (körbeérő) sáv."""
    total = sum(counts.values())
    if total < min_actions:
        return None
    per_day = [sum(v for (d, _), v in counts.items() if d == day) for day in range(7)]
    days = frozenset(d for d in range(7) if per_day[d] >= day_share * total)
    if not days:
        return None
    hours = [sum(v for (d, h), v in counts.items() if h == hour and d in days) for hour in range(24)]
    on_days = sum(hours)
    best: Optional[Tuple[int, int]] = None
    for length in range(1, 25):
        for start in range(24):
            covered = sum(hours[(start + i) % 24] for i in range(length))
            if covered >= coverage * on_days:
                best = (start, length)
                break
        if best:
            break
    start, length = best or (0, 24)
    # gyakorlatilag folyamatos: a sáv majdnem olyan széles, mint egyenletes (0-24) aktivitásnál lenne
    if length >= 0.8 * coverage * 24 and len(days) >= 6:
        schedule = Schedule(frozenset(range(7)), 0, 24, "tanult")
    else:
        end = (start + length) % 24 or 24
        schedule = Schedule(days, start, end, "tanult")
    inside = sum(v for (d, h), v in counts.items() if schedule.working_seconds(_slot(d, h), _slot(d, h) + timedelta(minutes=30)) > 0)
    return ScheduleSuggestion("", schedule, total, inside / total, (per_day[5] + per_day[6]) / total)


def _slot(weekday: int, hour: int) -> datetime:
    return datetime(2024, 1, 1 + weekday, hour, 15)   # 2024-01-01 hétfő


def compare_with_servicenow(pairs: Sequence[Tuple[float, float]]) -> Optional[Dict[str, float]]:
    """(a mi munkaidőnk, a ServiceNow business_stc) párokból: a medián arány és a darab (0 business_stc nélkül)."""
    ratios = sorted(ours / snow for ours, snow in pairs if snow > 0)
    if not ratios:
        return None
    return {"median_ratio": ratios[len(ratios) // 2], "count": len(ratios)}
