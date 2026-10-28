"""Backtest: a beültetett hibák mikor és mennyire zajosan jelennek meg a feltárásban és a diagnosztikákban.

Napról napra visszajátssza a mérést (a nap végén, as_of), elemzői döntések nélkül. Ha a hibák nélküli
(tiszta) változat is megvan ugyanarról a szintetikus adatról, ugyanazokon a napokon azt is méri, és:
- a hibának csak az a jelzés tulajdonítható, amelyik a tiszta futásban aznap nincs meg;
- a tiszta futás minden jelöltje téves riasztás (abban nincs hiba) – ez a zajszint.

Forgatókönyvenként:
- felismerés: az első nap a hiba kezdete után, amikor a hibának tulajdonítható jelzés megjelenik, és a késés;
- stabilitás: a hiba alatti napok hány részén jelez;
- természetes jelzés: a tiszta futás ugyanott jelzett-e (akkor a minta a hiba nélkül is ott van).

Felismerés forgatókönyv-fajtánként:
- slow_pickup / long_hold: feltárási jelölt a `stint_pickup` / `stint_hold` mérésen, a csoporttal a mintában;
  mindenkire (*) vonatkozó hibánál fordított a kérdés: hány csoport jelez emiatt (jó esetben alig, mert az
  alaparánnyal együtt nő minden csoport aránya);
- ping_pong: feltárási jelölt a `reassignments` mérésen a csoporttal, vagy PING_PONG issue a beültetett ticketeken;
- drop_feature: FEATURE_USAGE issue a csoportra (a funkciót elérhetőnek tekintve).
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set

from models.diagnostics.core import MeasurementContext, Trigger
from models.diagnostics.feature_usage import FeatureUsageDiagnosis
from models.diagnostics.ping_pong import PingPongDiagnosis
from models.discovery.clusters import decision_load
from models.discovery.config import DiscoveryConfig
from models.discovery.runner import discover
from models.knowledge_base import KnowledgeView

MEASURE_OF_KIND = {"slow_pickup": "stint_pickup", "long_hold": "stint_hold", "ping_pong": "reassignments"}


@dataclass
class ScenarioResult:
    name: str
    kind: str
    group: str
    start: datetime
    end: datetime
    applied: int
    first_detected: Optional[datetime] = None
    days_in_fault: int = 0          # mért napok a hiba alatt
    days_flagged: int = 0           # ebből a hibának tulajdonítható jelzés
    natural_days: int = 0           # a tiszta futás is jelzett ugyanott (a hiba alatt)
    natural_before: int = 0         # jelzés a hiba előtt (hibás futás; a tisztával egyezik)
    flagged_groups: Dict[str, int] = field(default_factory=dict)   # mindenkire vonatkozó hibánál

    @property
    def delay_days(self) -> Optional[float]:
        return (self.first_detected - self.start).total_seconds() / 86400 if self.first_detected else None

    @property
    def stability(self) -> float:
        return self.days_flagged / self.days_in_fault if self.days_in_fault else 0.0


def _has_group(result, group: str) -> bool:
    items = result.items + tuple(item for alternative in result.alternatives for item in alternative)
    return any(dim == "assignment_group" and value == group for dim, value in items)


@dataclass
class DaySignals:
    candidates: int
    hits: Dict[str, bool]
    groups: Dict[str, Set[str]]     # mindenkire vonatkozó forgatókönyvnél: a jelző csoportok
    unrelated: int
    decisions: int                  # elemzői döntés a jelöltekhez, ha a csomópontokat egyben döntjük el


def _day_signals(incidents, as_of, scenarios, truth_incidents, config, levels, knowledge, usage=None) -> DaySignals:
    results, _, _ = discover(incidents, as_of, config, levels)
    usage_hits: Set[str] = set()
    if any(s.kind == "drop_feature" for s in scenarios):
        usage = usage or {}
        ctx = MeasurementContext(Trigger.MANUAL, as_of, incidents, [], thresholds=usage.get("threshold", {}),
                                 window_params=usage.get("window", {}), knowledge=knowledge)
        usage_hits = {m.subject for m in FeatureUsageDiagnosis().run(ctx).measurements if m.exceeded}
    ping_hits: Set[str] = set()
    if any(s.kind == "ping_pong" for s in scenarios):
        ctx = MeasurementContext(Trigger.MANUAL, as_of, incidents, [], knowledge=knowledge)
        ping_hits = {m.subject for m in PingPongDiagnosis().run(ctx).measurements if m.exceeded}

    hits: Dict[str, bool] = {}
    groups: Dict[str, Set[str]] = {}
    for s in scenarios:
        if s.kind == "drop_feature":
            hits[s.name] = f"work_notes|{s.group}" in usage_hits
        elif s.group == "*":
            measure = MEASURE_OF_KIND[s.kind]
            groups[s.name] = {value for r in results if r.measure == measure for dim, value in r.items if dim == "assignment_group"}
        else:
            measure = MEASURE_OF_KIND[s.kind]
            hits[s.name] = any(r.measure == measure and _has_group(r, s.group) for r in results) or (
                s.kind == "ping_pong" and bool(ping_hits & truth_incidents[s.name]))
    fault_groups = {s.group for s in scenarios if s.group != "*"}
    unrelated = sum(1 for r in results if not any(_has_group(r, g) for g in fault_groups))
    entries = [(f"{r.measure}|{r.candidate_key}", r.measure, [r.items, *r.alternatives]) for r in results]
    return DaySignals(len(results), hits, groups, unrelated, decision_load(entries)["decisions"])


def run_backtest(
    incidents: Sequence[Any],
    manifest: Mapping[str, Any],
    config: Optional[DiscoveryConfig] = None,
    clean_incidents: Optional[Sequence[Any]] = None,
    first_day: Optional[datetime] = None,
    last_day: Optional[datetime] = None,
    step_days: int = 1,
    progress=None,
    usage: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """`usage`: a FEATURE_USAGE diagnosztika paraméterei ({"threshold": {...}, "window": {...}})."""
    config = config or DiscoveryConfig()
    levels = {manifest["l1"]: "L1"}
    # a funkciót elérhetőnek tekintjük: a hiány edukációs jelzés legyen, ne kérdés
    knowledge = KnowledgeView(levels=levels, availability={("work_notes", "*"): True})
    scenarios = [ScenarioResult(s["name"], s["kind"], s["group"], datetime.fromisoformat(s["start"]),
                                datetime.fromisoformat(s["end"]), s["applied"]) for s in manifest["scenarios"]]
    truth_incidents: Dict[str, set] = defaultdict(set)
    for item in manifest["truth"]:
        truth_incidents[item["fault"]].add(item["incident_id"])

    day = (first_day or min(s.start for s in scenarios) - timedelta(days=14)).replace(hour=0, minute=0, second=0, microsecond=0)
    last = last_day or max(s.end for s in scenarios) + timedelta(days=7)
    noise = {"faulty_unrelated": [], "clean_total": [], "clean_decisions": []}
    daily: List[Dict[str, Any]] = []

    while day <= last:
        as_of = datetime.combine(day.date(), time(23, 59, 59))
        faulty = _day_signals(incidents, as_of, scenarios, truth_incidents, config, levels, knowledge, usage)
        clean = _day_signals(clean_incidents, as_of, scenarios, truth_incidents, config, levels, knowledge, usage) \
            if clean_incidents is not None else None
        for s in scenarios:
            in_fault = s.start <= as_of < s.end
            if s.group == "*":
                if in_fault:
                    extra = faulty.groups[s.name] - (clean.groups[s.name] if clean else set())
                    for group in extra:
                        s.flagged_groups[group] = s.flagged_groups.get(group, 0) + 1
                continue
            natural = clean.hits[s.name] if clean else False
            attributable = faulty.hits[s.name] and not natural
            if as_of < s.start:
                s.natural_before += faulty.hits[s.name]
            elif in_fault or as_of < s.end + timedelta(days=7):
                s.days_in_fault += in_fault
                s.natural_days += natural and in_fault
                s.days_flagged += attributable and in_fault
                if attributable and s.first_detected is None:
                    s.first_detected = as_of
        noise["faulty_unrelated"].append(faulty.unrelated)
        if clean:
            noise["clean_total"].append(clean.candidates)
            noise["clean_decisions"].append(clean.decisions)
        daily.append({"day": as_of.date().isoformat(), "faulty": faulty.candidates,
                      "clean": clean.candidates if clean else None, "hits": faulty.hits,
                      "natural": clean.hits if clean else None})
        if progress:
            progress(as_of, faulty.candidates, clean.candidates if clean else None)
        day += timedelta(days=step_days)

    return {
        "scenarios": scenarios,
        "noise": {key: (sum(values) / len(values) if values else None) for key, values in noise.items()},
        "daily": daily,
        "has_clean": clean_incidents is not None,
    }


def report(result: Mapping[str, Any]) -> str:
    lines = [f"{'forgatókönyv':26}{'fajta':13}{'beült.':>7}{'kezdet':>12}{'felismerve':>12}{'késés':>7}{'stabilitás':>12}"
             f"{'term.':>7}  megjegyzés"]
    for s in result["scenarios"]:
        if s.group == "*":
            top = sorted(s.flagged_groups.items(), key=lambda kv: -kv[1])[:3]
            note = (f"{len(s.flagged_groups)} csoport jelzett miatta" +
                    (f" (pl. {', '.join(f'{g[:8]}: {n} nap' for g, n in top)})" if top else ""))
            lines.append(f"{s.name:26}{s.kind:13}{s.applied:>7}{s.start.date().isoformat():>12}{'-':>12}{'-':>7}{'-':>12}{'-':>7}  {note}")
            continue
        detected = s.first_detected.date().isoformat() if s.first_detected else "nem"
        delay = f"{s.delay_days:.0f}n" if s.delay_days is not None else "-"
        notes = []
        if s.natural_before:
            notes.append(f"a hiba előtt {s.natural_before} napon is jelzett")
        if s.natural_days:
            notes.append(f"a tiszta futás is jelzett a hiba alatt {s.natural_days} napon")
        lines.append(f"{s.name:26}{s.kind:13}{s.applied:>7}{s.start.date().isoformat():>12}{detected:>12}{delay:>7}"
                     f"{s.stability:>11.0%} {s.natural_days:>6}  {'; '.join(notes)}")
    noise = result["noise"]
    lines.append("")
    if noise.get("clean_total") is not None:
        lines.append(f"Téves riasztás (a tiszta futás jelöltjei naponta): {noise['clean_total']:.1f}, "
                     f"ebből elemzői döntés csomópontokkal: {noise['clean_decisions']:.1f}")
    lines.append(f"A hibákhoz nem köthető jelöltek naponta a hibás futásban: {noise['faulty_unrelated']:.1f}")
    return "\n".join(lines)
