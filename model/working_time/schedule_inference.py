from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Sequence, Tuple

from model.working_time.schedule import Schedule
from model.working_time.schedule_suggestion import ScheduleSuggestion


# Nem emberi műveletek (rendszer írja, vagy a lezáró mentés kísérői): az aktivitásba nem számítanak
SYSTEM_FIELDS = frozenset({"email", "u_reaction_time", "reassignment_count", "u_old_assignment_group", "business_duration",
                           "business_stc", "calendar_duration", "calendar_stc", "resolved_at", "resolved_by",
                           "u_strike_counter", "active"})

def activity(timelines: Iterable, since: datetime, until: datetime) -> Dict[str, Counter]:
    """Csoportonként a saját mentések (nem rendszer-műveletek) száma hét napja × óra szerint."""
    result: Dict[str, Counter] = defaultdict(Counter)
    for timeline in timelines:
        for stint in timeline.stints:
            saves = {c.at for c in timeline.own_changes(stint) if c.field not in SYSTEM_FIELDS and since <= c.at <= until}
            for at in saves:
                result[stint.group][(at.weekday(), at.hour)] += 1
    return result

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
