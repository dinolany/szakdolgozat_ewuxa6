"""Utókövetés: a lezárt issue után a gép utánanéz, hogy a megoldás tényleg működött-e.

Két fajta:
- improve („javulást várok”): újramérhető (csoportot, funkciót, mintát mérő) issue-nál. A határidőig figyeli a
  mérést; a határidőn: ha a legutóbbi mérés már nem lépi túl a küszöböt -> „javult”, különben -> „nem javult”, és az
  issue újranyílik.
- no_repeat („ne ismétlődjön”): bármely issue-nál, főleg jegyszintűnél. A határidőig figyeli, nyílik-e ugyanannál a
  diagnosztikánál ugyanarra a hatókörre (csoport, service…) új issue; ha igen -> „megismétlődött” (azonnal),
  különben a határidőn -> „nem ismétlődött”.

Ez a modul tiszta: állapotot értékel; az issue újranyitását és a tárolást a szolgáltatás végzi.
"""

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


MODES = {
    "improve": "Javulást várok",
    "no_repeat": "Ne ismétlődjön",
}

STATUS_LABELS = {"running": "folyamatban", "improved": "javult", "not_improved": "nem javult",
                 "repeated": "megismétlődött", "not_repeated": "nem ismétlődött"}

ATTENTION = {"not_improved", "repeated"}

def start(mode: str, days: int, now: str, issue: Mapping[str, Any], scope: Sequence[Sequence[str]] = ()) -> Dict[str, Any]:
    """Utókövetés indítása egy lezárt issue-ra (javulást várok / ne ismétlődjön), a határidővel és a hatókörrel.

    Hívja: DashboardService._page (hivatkozás), DiscoveryService._profiles (hivatkozás),
    DiscoveryService.close_issue_with_lesson, Fault.probability (hivatkozás), Interval.clip (hivatkozás),
    MeasurementWindow.from_values (hivatkozás), Schedule.working_hours (hivatkozás), Schedule.working_seconds
    (hivatkozás), build_synthetic.main (hivatkozás) és mások."""
    if mode not in MODES:
        raise ValueError(f"Ismeretlen utókövetés: {mode}")
    if not 1 <= int(days) <= 180:
        raise ValueError("Az utókövetés 1–180 napig tarthat.")
    if mode == "no_repeat" and not scope:
        raise ValueError("Jelöld be, mire figyeljen (pl. a csoport), hogy ne ismétlődjön.")
    started = datetime.fromisoformat(now)
    return {"mode": mode, "label": MODES[mode], "days": int(days), "started_at": now,
            "until": (started + timedelta(days=int(days))).isoformat(), "scope": [list(i) for i in scope],
            "baseline_metric": issue.get("metric_value"), "status": "running", "checks": [], "repeats": []}

def evaluate(follow_up: Mapping[str, Any], issue: Mapping[str, Any], now: datetime,
             repeats: Iterable[str] = ()) -> Tuple[str, Optional[str]]:
    """(új állapot, teendő): teendő "reopen" (nem javult: újranyitás) vagy None.

    Hívja: DiscoveryService._check_follow_ups."""
    if follow_up.get("status") != "running":
        return follow_up.get("status", "running"), None
    due = now >= datetime.fromisoformat(follow_up["until"])
    if follow_up["mode"] == "no_repeat":
        if list(repeats):
            return "repeated", None
        return ("not_repeated", None) if due else ("running", None)
    if not due:
        return "running", None
    exceeded = (issue.get("details") or {}).get("measured_exceeded")
    if exceeded is False:
        return "improved", None
    return "not_improved", "reopen"

def matching_repeats(follow_up: Mapping[str, Any], original_id: str, diagnostic: str,
                     issues: Iterable[Mapping[str, Any]], entity_of: Callable[[Mapping[str, Any]], Sequence[Tuple[str, str]]]) -> List[str]:
    """Az utókövetés kezdete óta nyílt, azonos diagnosztikájú issue-k, amelyek entitásai a hatókört lefedik.

    Hívja: DiscoveryService._check_follow_ups."""
    scope = {(str(d), str(v)) for d, v in follow_up.get("scope", [])}
    found = []
    for issue in issues:
        if issue.get("issue_id") == original_id or (issue.get("diagnostic_key") or issue.get("category")) != diagnostic:
            continue
        if (issue.get("first_detected_at") or issue.get("measured_at") or "") < follow_up["started_at"]:
            continue
        if scope <= {(str(d), str(v)) for d, v in entity_of(issue)}:
            found.append(issue["issue_id"])
    return found
