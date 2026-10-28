"""Tanítási szimuláció: a teljes tanuló kör napról napra, egy szimulált elemzővel, a beültetett hibákon.

A szintetikus adatnál ismert a megoldókulcs (hova, mikor, milyen hibát ültettünk), így a tanítási fázis
végigjátszható kézi kattintgatás nélkül. A szimulált elemző a mostani felület menetét követi (a jelöltnek három
művelete van, a tanulás az issue lezárásából és a kifejezett elnyomásból jön):

- a beültetett hibához tartozó jelölt: Feladat (issue nyílik); ISSUE_DAYS nap vizsgálat után „valódi hiba”
  tanulsággal lezárja (ez tanítja a tippet); amíg az issue nyitott, a mintájára illő új jelölt hozzá csatolódik;
- zaj: a csak időből álló minta (nap, óra) -> Bontsd tovább; a már NOT_NOW_LIMIT-szer „most nem”-mel visszatért
  minta -> Ez a minta soha (a jelölt oldaláról); különben -> Most nem;
- ha ugyanaz a mintaelem (mező = érték) sok elvetett zajban szerepel -> Ezt az elemet soha;
- családszintű döntés nincs: a tagokról egyenként dönt, minden jelölt egy döntés;
- hetente átnézi az általánosítási javaslatokat: elfogadja, ami nem takarna el valódi hibát.

Ugyanazon az adaton egy tanulás nélküli összehasonlító kör is fut (a zajra mindig csak „most nem”, nincs
szabály, bontás, csatolás és általánosítás): a kettő különbsége a tanulás hatása.

Mérések naponta: döntésre váró jelöltek, a felhasznált döntések, lezárások, csatolt jelöltek, a zaj aránya a
bemutatott sorban, a tipp egyezése a szimulált elemző döntésével; forgatókönyvenként: mikor jelent meg és mikor
lett feladat.
"""

from datetime import datetime, time, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from learning.discovery_config import DiscoveryConfig
from learning.discovery_manager import DiscoveryManager
from learning.feedback.generalize import suggestions as generalization_suggestions
from learning.mining.contrast_mining import with_implied
from learning.runner import configure, run_discovery
from learning.state.candidate import Candidate
from learning.state.candidate_state import CandidateState
from learning.state.rule import Rule
from learning.state.scope import Scope
from learning.state.verdict import Verdict
from model.diagnostics.core import MeasurementContext, Trigger
from model.diagnostics.detectors.feature_usage.feature_usage_diagnosis import FeatureUsageDiagnosis
from model.knowledge.knowledge_view import KnowledgeView
from simulation.experiments.track import Track
from simulation.experiments.tracked_fault import TrackedFault
from storage.knowledge_base import KnowledgeBase


MEASURE_OF_KIND = {"slow_pickup": "stint_pickup", "long_hold": "stint_hold", "ping_pong": "reassignments"}
TIME_DIMENSIONS = {"weekday", "hour", "date"}
# Ezekre szóló elvetés (csomópont vagy általánosítás) egy egész figyelést kapcsolna ki (pl. feature=work_notes
# minden csoportra); a figyelmes elemző ilyet nem tesz, ezért a szimulált sem
WHOLE_MONITOR_DIMENSIONS = {"feature"}
AFTER_FAULT = timedelta(days=7)   # a hiba vége után ennyi ideig még a hibához tartozónak számít
ISSUE_DAYS = 3                    # ennyi nap vizsgálat után zárja le a feladatot (valódi hiba tanulsággal)
NOT_NOW_LIMIT = 2                 # ennyi "most nem" után a visszatérő zaj: Ez a minta soha
ELEMENT_LIMIT = 4                 # ennyi elvetett zajban szereplő elem: Ezt az elemet soha


def _groups_of(candidate: Candidate) -> set:
    return {v for desc in candidate.descriptions() for d, v in with_implied(tuple(sorted(desc))) if d == "assignment_group"}


def fault_of(candidate: Candidate, faults: Sequence[TrackedFault], now: datetime) -> Optional[TrackedFault]:
    """A megoldókulcs: melyik beültetett hibához tartozik a jelölt (ha bármelyikhez)."""
    for fault in faults:
        if fault.group == "*" or not (fault.start <= now <= fault.end + AFTER_FAULT):
            continue
        if fault.kind == "drop_feature":
            if candidate.kind == "signal" and ("feature", "work_notes") in candidate.items and fault.group in _groups_of(candidate):
                return fault
        elif candidate.measure == MEASURE_OF_KIND.get(fault.kind) and fault.group in _groups_of(candidate):
            return fault
    return None


def noise_action(candidate: Candidate) -> str:
    dims = {d for d, _ in candidate.items}
    if candidate.kind == "contrast" and dims and dims <= TIME_DIMENSIONS:
        return "refine"
    if candidate.not_now >= NOT_NOW_LIMIT:
        return "never_pattern"
    return "reject_once"


def _knowledge(l1: str) -> KnowledgeBase:
    kb = KnowledgeBase()
    kb.add_fact("szint", {"csoport": l1, "szint": "L1"})
    kb.add_fact("elerheto", {"funkcio": "work_notes", "csoport": "*"})
    return kb


def _signals(incidents, as_of: datetime, knowledge: KnowledgeView, usage: FeatureUsageDiagnosis, window_days: int):
    ctx = MeasurementContext(Trigger.MANUAL, as_of, incidents, [], window_params={"lookback_minutes": window_days * 1440},
                             knowledge=knowledge, review=True)
    return [{"diagnostic": usage.key, "subject": o.subject, "items": usage.signal_items(o.subject, o.details),
             "details": dict(o.details), "severity": s.value, "description": usage.describe(o.subject, o.details),
             "affected": o.affected} for o, s in usage.signals(ctx)]


def _decide(track: Track, candidate: Candidate, action: str, today: str) -> None:
    m = track.manager
    if action == "finding":
        m.decide(candidate.candidate_id, Verdict.FINDING.value)
        if track.learning:   # a feladatból issue lesz; a tanuló kör lezárja és csatol hozzá
            issue_id = f"SIM-{candidate.candidate_id}"
            m.link_issue(candidate.candidate_id, issue_id)
            track.open_issues[issue_id] = {"candidate_id": candidate.candidate_id, "opened": today}
    elif action == "refine":
        m.decide(candidate.candidate_id, Verdict.REFINE.value)
    elif action == "never_pattern":
        # a jelölt oldaláról: Ez a minta soha (kemény szabály)
        decision = m.decide(candidate.candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value)
        rule = Rule.from_dict(m.state["rules"][decision.rule_id])
        rule.hard = True
        m._save_rule(rule)
        m.apply_rules(count_hits=False)
    else:
        m.decide(candidate.candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value)


def _close_issues(track: Track, as_of: datetime) -> int:
    """A vizsgálati idő után a feladat lezárása „valódi hiba” tanulsággal (ez a tipp erős címkéje)."""
    m, closed = track.manager, 0
    for issue_id, info in list(track.open_issues.items()):
        if (as_of.date() - datetime.fromisoformat(info["opened"]).date()).days >= ISSUE_DAYS:
            m.decide(info["candidate_id"], Verdict.FINDING.value, "confirmed", note=f"{issue_id} lezárása: valódi hiba")
            del track.open_issues[issue_id]
            closed += 1
    if closed:
        m.release_attached(set(track.open_issues))
    return closed


def _suppress_elements(track: Track, candidate: Candidate, fault_values: set) -> int:
    """A visszatérő zajelem: ha sok elvetett zajban szerepel (és nem egy hibás csoport), Ezt az elemet soha."""
    made = 0
    for dim, value in candidate.items:
        if dim in TIME_DIMENSIONS or dim in WHOLE_MONITOR_DIMENSIONS or value in fault_values or ":" in dim:
            continue
        key = (dim, value)
        track.noise_elements[key] = track.noise_elements.get(key, 0) + 1
        if track.noise_elements[key] == ELEMENT_LIMIT:
            track.manager.add_rule([{"dim": dim, "values": [value]}], reason="szimulált elemző: ezt az elemet soha",
                                   source="element:sim", hard=True)
            track.suppressed_elements.append(f"{dim}={value}")
            made += 1
    return made


def _day(track: Track, incidents, as_of: datetime, config: DiscoveryConfig, faults: Sequence[TrackedFault],
         signals: List[Dict[str, Any]], record_faults: bool) -> Dict[str, Any]:
    m = track.manager
    report = run_discovery(m, incidents, as_of, config)
    m.ingest_signals(signals, as_of)
    waiting = m.queue(10_000)
    today = as_of.date().isoformat()
    log = track.fault_log
    for c in waiting:
        fault = fault_of(c, faults, as_of)
        if fault:
            log.setdefault(fault.name, {}).setdefault("first_seen", today)
            if record_faults and fault.first_seen is None:
                fault.first_seen = today

    capacity = m.capacity()
    decisions = findings = noise_shown = shown = element_rules = 0
    closed = attached = 0
    fault_values = {f.group for f in faults} | {"work_notes"}
    if track.learning:
        closed = _close_issues(track, as_of)
        attached = len(m.attach_to_open_issues(set(track.open_issues)))
    # a napi keret: minden jelölt egy döntés (a családokon nincs közös művelet)
    for candidate in m.queue(config.get("queue", "daily_limit"), max(0, capacity["remaining"])):
        candidate = m.candidate(candidate.candidate_id)
        if candidate.state != CandidateState.QUEUED.value:
            continue
        fault = fault_of(candidate, faults, as_of)
        action = "finding" if fault else (noise_action(candidate) if track.learning else "reject_once")
        if candidate.suggestion:
            track.tips.append((candidate.suggestion["verdict"] == Verdict.FINDING.value) == (action == "finding"))
        if fault:
            log.setdefault(fault.name, {}).setdefault("first_shown", today)
        if record_faults and fault and fault.first_shown is None:
            fault.first_shown = as_of.date().isoformat()
        _decide(track, candidate, action, today)
        decisions += 1
        shown += 1
        if fault:
            findings += 1
            log.setdefault(fault.name, {}).setdefault("accepted", today)
            if record_faults and fault.accepted is None:
                fault.accepted = as_of.date().isoformat()
        else:
            noise_shown += 1
            if track.learning and action != "refine":
                made = _suppress_elements(track, candidate, fault_values)
                element_rules += made
                decisions += made   # az elem elnyomása is egy kattintás
    # 3. hetente az általánosítási javaslatok (ami nem takarna el valódi hibát)
    if track.learning and as_of.weekday() == 6:
        for suggestion in generalization_suggestions(m.rules(), m.knowledge.store.get("breakdown", {}),
                                                     config.dimension_roles(), {}, config.generalize_min_count()):
            if suggestion["kind"] == "general_rule" and suggestion["dimension"] in WHOLE_MONITOR_DIMENSIONS:
                continue   # egy egész funkciófigyelés kikapcsolása lenne
            if suggestion["kind"] == "general_rule" and suggestion.get("value") not in fault_values:
                m.add_rule([{"dim": suggestion["dimension"], "values": [suggestion["value"]]}],
                           measures=suggestion["measures"] or None, reason="szimulált elemző: általánosítás",
                           source="generalization")
                track.generalizations.append({"day": as_of.date().isoformat(), "title": suggestion["title"]})
            elif suggestion["kind"] == "dimension_role":
                config.values["dimensions"][suggestion["dimension"]] = suggestion["role"]
                track.generalizations.append({"day": as_of.date().isoformat(), "title": suggestion["title"]})

    recent = track.tips[-40:]
    row = {
        "day": as_of.date().isoformat(), "waiting": len(waiting), "decisions": decisions, "closed": closed, "attached": attached,
        "element_rules": element_rules, "open_issues": len(track.open_issues),
        "shown": shown, "noise_shown": noise_shown, "findings": findings, "capacity": capacity["capacity"],
        "tip_agreement": sum(recent) / len(recent) if recent else None,
        "rules": sum(1 for r in m.rules() if r.state == "active"), "new": len(report.new),
    }
    track.daily.append(row)
    return row


def run_learning_sim(
    incidents: Sequence[Any],
    manifest: Mapping[str, Any],
    config_path: Optional[str] = None,
    first_day: Optional[datetime] = None,
    last_day: Optional[datetime] = None,
    with_baseline: bool = True,
    signal_window_days: int = 14,
    progress: Optional[Callable[[Dict[str, Any], Optional[Dict[str, Any]]], None]] = None,
) -> Dict[str, Any]:
    l1 = manifest["l1"]
    faults = [TrackedFault(s["name"], s["kind"], s["group"], datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
              for s in manifest["scenarios"]]
    learning_config = DiscoveryConfig(config_path) if config_path else DiscoveryConfig()
    baseline_config = DiscoveryConfig(config_path) if config_path else DiscoveryConfig()
    for cfg in (learning_config, baseline_config):
        cfg.save = lambda: None   # a szimuláció nem írja a valódi beállításfájlt
    tracks = [Track("tanuló", True, DiscoveryManager({}, knowledge=_knowledge(l1)))]
    if with_baseline:
        tracks.append(Track("tanulás nélkül", False, DiscoveryManager({}, knowledge=_knowledge(l1))))
    for track, cfg in zip(tracks, (learning_config, baseline_config)):
        configure(track.manager, cfg)
    usage = FeatureUsageDiagnosis(None)
    knowledge_view = tracks[0].manager.knowledge_view()

    day = first_day or (min(f.start for f in faults) - timedelta(days=21))
    last = last_day or (max(f.end for f in faults) + timedelta(days=7))
    day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= last:
        as_of = datetime.combine(day.date(), time(23, 59, 59))
        signals = _signals(incidents, as_of, knowledge_view, usage, signal_window_days)
        rows = []
        for track, cfg in zip(tracks, (learning_config, baseline_config)):
            rows.append(_day(track, incidents, as_of, cfg, faults, signals, record_faults=track.learning))
        if progress:
            progress(rows[0], rows[1] if len(rows) > 1 else None)
        day += timedelta(days=1)

    return {
        "faults": [{**f.__dict__, "start": f.start.isoformat(), "end": f.end.isoformat()} for f in faults],
        "tracks": {t.name: {"daily": t.daily, "generalizations": t.generalizations, "faults": t.fault_log,
                            "decisions": sum(r["decisions"] for r in t.daily),
                            "closed": sum(r["closed"] for r in t.daily), "attached": sum(r["attached"] for r in t.daily),
                            "suppressed_elements": t.suppressed_elements,
                            "rules": t.daily[-1]["rules"] if t.daily else 0} for t in tracks},
        "period": {"first": tracks[0].daily[0]["day"] if tracks[0].daily else None,
                   "last": tracks[0].daily[-1]["day"] if tracks[0].daily else None},
    }
