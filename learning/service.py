"""A feltárási ("tanuló") kör szolgáltatása a dashboardnak és a szimulációnak.

Egy helyen fogja össze a feltárás állapotát (jelöltek, döntések, szabályok), a paramétereit és a közös
tudástárat, és a fő elemzővel együtt dolgozik:
- a szimuláció kötegei után virtuális naponta egyszer magától fut (after_batch), és kézzel is indítható;
- a finding a fő adatbázisba kerül PATTERN_FINDING issue-ként (az elemző diagnosztikájával mérve);
- a tudás (tények) változása után az elemző újramér, mert a tények a diagnosztikák eredményét is alakítják.

Minden művelet a szolgáltatás zárja alatt fut, és utána menti az állapotot.
"""

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core import Trigger
from model.diagnostics.detectors.feature_usage import DEFAULT_CATALOG
from learning.analysis import DIMENSION_LABELS, analyze_candidate, breakdown, brief, describe_condition, _value_text
from learning.generalize import suggestions as generalization_suggestions
from learning.backlog import (affected_arrivals as backlog_affected, calendar as backlog_cells,
                                      candidate_groups as backlog_candidate_groups, group_stints as backlog_group_stints,
                                      time_marks as backlog_time_marks)
from learning import follow_up as follow_ups
from storage import backup as backups
from learning.learned_thresholds import suggestions as learned_threshold_suggestions
from learning.lessons import OUTCOMES as LESSON_OUTCOMES, describe_lesson, lesson_options
from model.working_time.schedule import (Schedule, activity as working_activity, compare_with_servicenow, fact_args as working_fact_args,
                                 infer_schedule)
from learning.children import (ROLES as CHILD_ROLES, ChildDef, compile_expression, review_children,
                                       rows_to_expression, run_child, run_probes, trigger_label, trigger_options, unit_ref,
                                       vocabulary, GROUP_PROBE_DIMENSIONS, GROUP_PROBES, METHODS as CHILD_METHODS, PROBES)
from learning.config import (DEFINITIONS, DIMENSION_ROLE_DEFAULTS, DIMENSION_ROLE_LABELS, DIMENSIONS_SECTION, MEASURES_SECTION,
                                     DiscoveryConfig)
from learning.knowledge import Candidate, CandidateState, Rule, Scope, Verdict
from learning.manager import DiscoveryManager
from learning.measures import MEASURES_BY_KEY
from learning.mining import path_blocked, pattern_key, with_implied
from learning.profile import build_profiles, has_value
from learning.promotion import promote_finding
from learning.review import review_facts, review_rules
from learning.runner import backfill_counts, configure, run_discovery
from model.knowledge.knowledge_base import PREDICATES
from model.timeline.ticket_timeline import incident_timeline

# A jelölt három művelete (a tudást adó döntések az elnyomás és az issue-lezárás tanulsága)
DECISIONS = ("finding", "reject_once", "refine")


def _measure_label(key: str) -> str:
    """Egy mérés neve (a minták méréseinél); a detektorokét a DiscoveryService._key_label adja.

    Hívja: DiscoveryService._key_label."""
    measure = MEASURES_BY_KEY.get(key)
    return measure.label if measure else key


class DiscoveryService:
    def __init__(self, analyzer: Any, repository: Any, state_path: str = "discovery_state.json",
                 config_path: str = "config/discovery.json"):
        """A tanuló modul szolgáltatása: a feltárási állapot betöltése, a kezelő és a közös tudástár; a tanult elvető
        szabályokat a gyári detektorok elé köti.

        Létrehozza: dashboard_server, main."""
        self.analyzer = analyzer
        self.repository = repository
        self.knowledge = analyzer.knowledge  # a fő elemzővel közös tudástár
        self.state_path = Path(state_path)
        self.config = DiscoveryConfig(config_path)
        self._lock = threading.RLock()
        state = self._load_state()
        # Régi állapot: a tények még a feltárási állapotban voltak; átkerülnek a közös tudástárba
        self.knowledge.import_facts(state.pop("facts", {}) or {})
        self.manager = DiscoveryManager(state, knowledge=self.knowledge)
        configure(self.manager, self.config)
        # korábban visszavont szabályok elvetett jelöltjei (a visszavonás eddig nem hozta vissza őket)
        if self.manager.reopen_rejected_by_revoked_rules():
            self._save()
        # a tanult elvető szabályok a gyári detektorok valós idejű issue-nyitására is hatnak
        analyzer.rule_provider = self.detector_rules

    def detector_rules(self) -> List[Dict[str, Any]]:
        """Az aktív elvető szabályok, amelyek egy gyári detektorra szólnak (a mérés-szűrőjükben a detektor kulcsa).

        Hívja: DiscoveryService.__init__ (átadva)."""
        now = self.manager.now
        result = []
        for data in list(self.manager.state.get("rules", {}).values()):
            rule = Rule.from_dict(data)
            if rule.action != "drop" or not rule.is_active(now) or rule.fact_binding or rule.measure_filter.get("mode") != "list":
                continue
            measures = [m for m in rule.measure_filter.get("measures", []) if self.analyzer.diagnostic_registry.get(m)]
            if measures and rule.conditions:
                result.append({"rule_id": rule.rule_id, "conditions": rule.conditions, "measures": measures})
        return result

    # ------------------------------------------------------------------
    # Állapot
    # ------------------------------------------------------------------

    def _load_state(self) -> Dict[str, Any]:
        """A feltárási állapot betöltése a fájlból.

        Hívja: DiscoveryService.__init__, DiscoveryService.backup_action."""
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        return {}

    def _save(self) -> None:
        # a teljes állapot megmarad (pl. a konzol saját "findings" része is, amelyet ez a szolgáltatás nem használ)
        """A feltárási állapot mentése (atomikusan), utána a tudás másolata.

        Hívja: DiscoveryService.__init__, DiscoveryService._mutate, DiscoveryService._run,
        DiscoveryService.availability, DiscoveryService.breakdown_feedback, DiscoveryService.children_action,
        DiscoveryService.close_issue_with_lesson, DiscoveryService.decide, DiscoveryService.generalize és mások."""
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.manager.state, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        temp.replace(self.state_path)
        self._backup()

    @property
    def backup_dir(self) -> Path:
        """A tudás-mentések mappája (a feltárási állapot mellett).

        Hívja: DiscoveryService._backup (átadva), DiscoveryService.backup_action (átadva),
        DiscoveryService.backup_overview (átadva)."""
        return self.state_path.parent / "backups"

    def _memory_files(self) -> List[Path]:
        """A tanult tudást tartó fájlok: a feltárási állapot és a tudástár.

        Hívja: DiscoveryService._backup, DiscoveryService.backup_action."""
        return [p for p in (self.state_path, getattr(self.knowledge, "path", None)) if p is not None]

    def _backup(self, force: bool = False, label: str = "") -> Optional[str]:
        """Datált másolat a tanult tudásról (tudástár + feltárási állapot); hiba esetén a mentés nem áll meg miatta.

        Hívja: DiscoveryService._save, DiscoveryService.after_batch, DiscoveryService.backup_action,
        DiscoveryService.reset."""
        try:
            return backups.snapshot(self._memory_files(), self.backup_dir, force=force, label=label)
        except Exception as error:
            print(f"--> [MENTÉS] a tudás másolata nem készült el: {error}", flush=True)
            return None

    def backup_overview(self) -> Dict[str, Any]:
        """A mentések listája a Tudás fülhöz.

        Hívja: DiscoveryService.knowledge_overview."""
        return {"snapshots": backups.list_snapshots(self.backup_dir), "dir": str(self.backup_dir)}

    def backup_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Mentés kézzel, vagy visszaállítás egy mentésre (előtte a mostani állapot is mentődik).

        Hívja: felület (POST /api/discovery/backup)."""
        op = payload.get("op")
        with self._lock:
            if op == "create":
                name = self._backup(force=True, label="kezi")
                return {"name": name, "message": f"Mentés kész: {name}."}
            if op != "restore":
                raise ValueError("A művelet: create | restore")
            name = str(payload.get("name", ""))
            self._backup(force=True, label="visszaallitas-elott")   # a mostani állapot se vesszen el
            restored = backups.restore(self.backup_dir, name, self._memory_files())
            self.knowledge.reload()
            self.manager = DiscoveryManager(self._load_state(), knowledge=self.knowledge)
            configure(self.manager, self.config)
        with self.repository.transaction() as db_data:
            self.analyzer.reanalyze_all(db_data)
        return {"restored": restored, "message": f"Visszaállítva a(z) {name} mentésre ({', '.join(restored)}). "
                                                 f"Az előző állapot is mentve lett („visszaallitas-elott”)."}

    def _fresh_manager(self) -> DiscoveryManager:
        """Új feltárási állapot (pl. a lejátszás újraindult): a jelöltek és a döntések törlődnek, de a szabályok (elnyomások,
        „nem hiba” szabályok) és az azonosító-számlálók megmaradnak – azok tanult tudás, nem az adathoz kötött állapot.

        Hívja: DiscoveryService.after_batch, DiscoveryService.reset."""
        old = self.manager.state
        state = {"rules": dict(old.get("rules", {})), "meta": {"counters": dict(old.get("meta", {}).get("counters", {}))}}
        manager = DiscoveryManager(state, knowledge=self.knowledge)
        configure(manager, self.config)
        return manager

    def reset(self) -> None:
        """A szimuláció újraindításakor: a jelöltek és a döntések törlődnek; a tudástár és a szabályok megmaradnak.

        Hívja: DashboardService.update_simulation."""
        with self._lock:
            self._backup(force=True, label="reset-elott")
            self.manager = self._fresh_manager()
            self._save()

    def _incidents(self, db_data: Optional[Dict[str, Any]] = None):
        """Az incidensek az adatbázisból.

        Hívja: DiscoveryService._brief_queue, DiscoveryService._now, DiscoveryService._run,
        DiscoveryService._threshold_context, DiscoveryService._unit_sets, DiscoveryService.analysis,
        DiscoveryService.backlog_calendar, DiscoveryService.breakdown_feedback,
        DiscoveryService.close_issue_with_lesson és mások."""
        if db_data is None:
            db_data = self.repository._load_db()
        return self.analyzer._incidents(db_data)

    def _now(self, incidents=None) -> datetime:
        """A mérés ideje: a legutóbbi feltárásé, különben a legutolsó eseményé.

        Hívja: DiscoveryService._accept_signal, DiscoveryService._threshold_context, DiscoveryService.analysis,
        DiscoveryService.availability, DiscoveryService.backlog_calendar, DiscoveryService.breakdown_feedback,
        DiscoveryService.children_action, DiscoveryService.close_issue_with_lesson, DiscoveryService.decide és mások."""
        if self.manager.now:
            return datetime.fromisoformat(self.manager.now)
        return self.analyzer._latest_event_time(incidents if incidents is not None else self._incidents())

    def _mutate(self, action: Callable[[], Any], reanalyze: bool = False) -> Any:
        """Művelet a zár alatt, mentéssel; tudásváltozásnál az elemző újramér a fő adatbázison.

        Hívja: DiscoveryService._suppress, DiscoveryService.availability, DiscoveryService.breakdown_feedback,
        DiscoveryService.children_action, DiscoveryService.fact, DiscoveryService.rule,
        DiscoveryService.schedule_action, DiscoveryService.unrefine."""
        with self._lock:
            result = action()
            self._save()
        if reanalyze:
            with self.repository.transaction() as db_data:
                self.analyzer.reanalyze_all(db_data)
        return result

    # ------------------------------------------------------------------
    # Futás
    # ------------------------------------------------------------------

    def after_batch(self, db_data: Dict[str, Any], measured_at: datetime) -> Optional[Dict[str, Any]]:
        """A szimuláció kötegei után: virtuális naponta egyszer fut (az első köteg után, amely új napra lép).

        Hívja: IncidentEventProcessor.process_events."""
        with self._lock:
            last = self.manager.state["meta"].get("last_run", {}).get("measured_at")
            if last and datetime.fromisoformat(last) - measured_at > timedelta(days=1):
                # az idő visszaugrott: egy új lejátszás indult; a régi jelöltek egy már nem létező adatra vonatkoznak
                print(f"--> [FELTÁRÁS] Az idő visszaugrott ({last[:16]} -> {measured_at:%Y-%m-%d %H:%M}): "
                      f"a feltárás állapota újraindul (a tudástár és a szabályok megmaradnak).", flush=True)
                self._backup(force=True, label="ujraindulas-elott")
                self.manager = self._fresh_manager()
                last = None
            if last and datetime.fromisoformat(last).date() >= measured_at.date():
                return None
            return self._run(db_data, measured_at)

    def run(self, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        """Kézi futtatás: alapból a legutolsó feldolgozott esemény (virtuális) idejében.

        Hívja: main (modulszint), felület (POST /api/discovery/run)."""
        with self.repository.transaction() as db_data:
            with self._lock:
                incidents = self._incidents(db_data)
                return self._run(db_data, as_of or self.analyzer._latest_event_time(incidents))

    def _run(self, db_data: Dict[str, Any], as_of: datetime) -> Dict[str, Any]:
        """A napi tanuló kör: feltárás, jelzések, összefoglalók és bevált szempontok, almérések, próbálkozások,
        csatolás, utókövetés, ismert hiba, mentés.

        Hívja: DiscoveryService.after_batch, DiscoveryService.run."""
        configure(self.manager, self.config)
        report = run_discovery(self.manager, self._incidents(db_data), as_of, self.config)
        signals = self._ingest_signals(db_data, as_of)
        self._brief_queue(db_data, as_of)
        self._run_children(db_data, as_of)
        self._run_probes(db_data, as_of)
        self._attach_to_open_issues(db_data, as_of)
        self._check_follow_ups(db_data, as_of)
        self._check_known_faults(db_data, as_of)
        self._save()
        return {"signals": signals,"measured_at": report.measured_at, "new": len(report.new), "updated": len(report.updated),
                "filtered": len(report.filtered), "returned": len(report.returned), "exceeds_baseline": len(report.exceeds_baseline)}

    def _ingest_signals(self, db_data: Dict[str, Any], as_of: datetime) -> int:
        """A jóváhagyásos diagnosztikák (pl. FEATURE_USAGE) jelzései a jelöltsorba.

        Hívja: DiscoveryService._run, DiscoveryService.availability."""
        manager = self.analyzer.threshold_manager
        signals = []
        for config_key in sorted(manager.review_keys - manager.disabled):
            diagnostic, ctx = self.analyzer.review_context(db_data, config_key, as_of)
            for observation, severity in diagnostic.signals(ctx):
                signals.append({
                    "diagnostic": diagnostic.key, "subject": observation.subject,
                    "items": diagnostic.signal_items(observation.subject, observation.details),
                    "details": dict(observation.details), "severity": severity.value,
                    "description": diagnostic.describe(observation.subject, observation.details),
                    "affected": observation.affected,
                })
        self.manager.ingest_signals(signals, as_of)
        return len(signals)

    def _profiles(self, incidents: Any, as_of: datetime, window_start: Optional[datetime] = None):
        """Az adatlapok (ticket és szakasz) a mérés pillanatában, a tudás szerinti szintekkel, keresési szempontokkal és
        munkaidővel. Hívja: _brief_queue, _unit_sets, decide (bontás), breakdown_feedback."""
        view = self.knowledge.view(as_of.isoformat())
        start = window_start or as_of - timedelta(days=self.config.get("queue", "lookback_days"))
        return build_profiles(incidents, as_of, start, view.levels, view.derived, view.schedules)

    def _refresh_breakdown(self, candidate: Candidate, profiles: Any, as_of: datetime) -> None:
        """A tovább bontott jelölt „Mi jellemző még?” bontása (stats.breakdown) a mostani adatlapokból.
        Hívja: _brief_queue (naponta) és decide (a bontás pillanatában)."""
        lookback = self.config.get("queue", "lookback_days")
        candidate.stats["breakdown"] = breakdown(candidate, profiles, self.config.measure_threshold(candidate.measure),
                                                 as_of - timedelta(days=lookback))
        candidate.stats["breakdown"]["measured_at"] = as_of.isoformat()
        self.manager._save_candidate(candidate)

    def _good_dimension_insights(self, candidates: Iterable[Candidate], profiles: Any, as_of: datetime) -> int:
        """A mérésnél bevált szempontok (Jó szempont) minden ilyen mérésű jelöltön: mezőnként az érintettek leggyakoribb
        értéke és aránya, a minta összes esetéhez képest (stats.good_dims a kártyára).

        Hívja: DiscoveryService._brief_queue, DiscoveryService.breakdown_feedback."""
        lookback = self.config.get("queue", "lookback_days")
        done = 0
        for candidate in candidates:
            dims = self.manager.good_dimensions(candidate.measure) if candidate.measure in MEASURES_BY_KEY else []
            pattern_dims = {d for d, _ in candidate.items}
            dims = [d for d in dims if d not in pattern_dims]
            if not dims:
                continue
            try:
                result = breakdown(candidate, profiles, self.config.measure_threshold(candidate.measure), as_of - timedelta(days=lookback),
                                   per_dimension=1, top=1000, min_difference=-1.0)
                by_dim = {r["dimension"]: r for r in result["rows"]}
                stored = self.manager.candidate(candidate.candidate_id)
                stored.stats["good_dims"] = [{"dimension": d, "label": self._name_children(by_dim[d]["label"]), "value_text": by_dim[d]["value_text"],
                                              "share_in_bad": by_dim[d]["share_in_bad"], "share_in_pattern": by_dim[d]["share_in_pattern"],
                                              "bad_count": by_dim[d]["bad_count"], "bad_total": by_dim[d]["bad_total"]}
                                             for d in dims if d in by_dim]
                self.manager._save_candidate(stored)
                done += 1
            except Exception as error:   # egy jelölt nem akaszthatja meg a többit
                print(f"--> [FELTÁRÁS] {candidate.candidate_id} bevált szempontjai nem készültek el: {error}", flush=True)
        return done

    def _brief_queue(self, db_data: Dict[str, Any], as_of: datetime) -> None:
        """A döntésre váró jelöltek tömör elemzése (a kártyára): naponta egyszer, egy közös adatlap-készleten.

        Hívja: DiscoveryService._run."""
        pool = [c for c in self.manager._open_pool() if c.kind != "signal" and c.measure in MEASURES_BY_KEY
                and c.window_end and datetime.fromisoformat(c.window_end) == as_of]
        refined = [c for c in self.manager.candidates() if c.state == CandidateState.REFINED.value and c.measure in MEASURES_BY_KEY]
        if not pool and not refined:
            return
        incidents = self._incidents(db_data)
        lookback = self.config.get("queue", "lookback_days")
        levels = self.knowledge.view(as_of.isoformat()).levels
        profiles = self._profiles(incidents, as_of)
        for candidate in pool:
            try:
                analysis = analyze_candidate(candidate, incidents, as_of, lookback, self.config.measure_threshold(candidate.measure),
                                             levels, trend_days=lookback, span_profiles=profiles)
                candidate.stats["brief"] = self._name_children(brief(analysis))
                self.manager._save_candidate(candidate)
            except Exception as error:  # egy jelölt összefoglalója nem akaszthatja meg a feltárást
                print(f"--> [FELTÁRÁS] {candidate.candidate_id} összefoglalója nem készült el: {error}", flush=True)
        # a mérésnél bevált szempontok minden ilyen mérésű jelöltön (a kártyán „Bevált szempontok”)
        self._good_dimension_insights(pool + refined, profiles, as_of)
        # a tovább bontott minták: mi jellemző az érintett eseteikre a minta összes esetéhez képest
        for candidate in refined:
            try:
                self._refresh_breakdown(candidate, profiles, as_of)
            except Exception as error:
                print(f"--> [FELTÁRÁS] {candidate.candidate_id} bontása nem készült el: {error}", flush=True)

    # ------------------------------------------------------------------
    # Almérések
    # ------------------------------------------------------------------

    def _unit_sets(self, db_data: Dict[str, Any], as_of: datetime, cache: Dict[str, Any]):
        """Egy jelölt esethalmazai (érintett, összehasonlító) – ugyanarra a pillanatra gyorsítótárral.

        Hívja: DiscoveryService._run_children, DiscoveryService._run_probes, DiscoveryService.children_action."""
        if "_incidents" not in cache:
            cache["_incidents"] = self._incidents(db_data)
            cache["_by_id"] = {incident.id: incident for incident in cache["_incidents"]}
        levels = self.knowledge.view(as_of.isoformat()).levels

        def for_candidate(candidate: Candidate):
            if candidate.kind == "signal":
                if "signals" not in cache:
                    cache["signals"] = {}
                    manager = self.analyzer.threshold_manager
                    for config_key in sorted(manager.review_keys - manager.disabled):
                        diagnostic, ctx = self.analyzer.review_context(db_data, config_key, as_of)
                        cache["signals"][diagnostic.key] = diagnostic.child_units(ctx)
                return cache["signals"].get(candidate.measure, {}).get(str(candidate.stats.get("subject")), ([], []))
            if candidate.measure not in MEASURES_BY_KEY:
                return [], []
            lookback = self.config.get("queue", "lookback_days")
            if "profiles" not in cache:
                cache["profiles"] = self._profiles(cache["_incidents"], as_of)
            measure = MEASURES_BY_KEY[candidate.measure]
            threshold = self.config.measure_threshold(candidate.measure)
            window_start = datetime.fromisoformat(candidate.window_start) if candidate.window_start else as_of - timedelta(days=lookback)
            affected, reference = [], []
            for profile in cache["profiles"].for_measure(measure):
                value = profile.measures.get(measure.key)
                if value is None or profile.opened_at < window_start:
                    continue
                in_pattern = all(has_value(profile.dimensions, d, v) for d, v in candidate.items)
                (affected if in_pattern and measure.is_bad(value, threshold) else reference).append(unit_ref(profile))
            return affected, reference

        def timeline_of(incident_id: str):
            incident = cache["_by_id"].get(incident_id)
            return incident_timeline(incident, as_of) if incident is not None else None

        return for_candidate, timeline_of, levels

    def _run_children(self, db_data: Dict[str, Any], as_of: datetime, candidates: Optional[List[Candidate]] = None) -> int:
        """A kiváltóikra illő almérések lefuttatása a döntésre váró jelölteken (az eredmény a kártyára kerül).

        Hívja: DiscoveryService._run, DiscoveryService.children_action, DiscoveryService.decide."""
        if not self.manager.children(active_only=True):
            return 0
        pool = candidates if candidates is not None else self.manager.active_candidates()
        cache: Dict[str, Any] = {}
        for_candidate, timeline_of, levels = self._unit_sets(db_data, as_of, cache)
        schedules = self.knowledge.view(as_of.isoformat()).schedules
        ran = 0
        for candidate in pool:
            children = self.manager.matching_children(candidate)
            if not children:
                continue
            try:
                affected, reference = for_candidate(candidate)
                results = [run_child(child, affected, reference, timeline_of, levels, as_of.isoformat(), schedules) for child in children]
                before = {r.get("child_id") for r in candidate.stats.get("children") or [] if r.get("triggered")}
                self.manager.store_child_results(candidate.candidate_id, results)
                ran += len(results)
                # tovább bontott jelölt: ha egy riasztó almérés most teljesül először, újra döntésre vár
                if candidate.state == CandidateState.REFINED.value and any(
                        r["triggered"] and r["role"] == "alert" and r["child_id"] not in before for r in results):
                    stored = self.manager.candidate(candidate.candidate_id)
                    stored.state = CandidateState.QUEUED.value
                    stored.flags = sorted(set(stored.flags) | {"child_alert"})
                    self.manager._save_candidate(stored)
            except Exception as error:  # egy almérés nem akaszthatja meg a feltárást
                print(f"--> [FELTÁRÁS] {candidate.candidate_id} almérése nem futott le: {error}", flush=True)
        auto = self.manager.auto_decide_by_children()
        if auto:
            print(f"--> [FELTÁRÁS] {len(auto)} jelöltet almérés döntött el (most nem).", flush=True)
        self.manager.refresh_suggestions()
        return ran

    def _run_probes(self, db_data: Dict[str, Any], as_of: datetime, candidates: Optional[List[Candidate]] = None) -> int:
        """A gép próbálkozásai a tovább bontott jelölteken: a gyári próbák és a máshoz tanított almérések, amelyek erre a
        jelöltre még nem futnak. A leginkább eltérők a kártyára kerülnek (stats.probes), visszajelzésre.

        Hívja: DiscoveryService._run, DiscoveryService.decide."""
        pool = candidates if candidates is not None else [c for c in self.manager.candidates() if c.state == CandidateState.REFINED.value]
        if not pool:
            return 0
        cache: Dict[str, Any] = {}
        for_candidate, timeline_of, levels = self._unit_sets(db_data, as_of, cache)
        schedules = self.knowledge.view(as_of.isoformat()).schedules
        children = self.manager.children(active_only=True)
        count = 0
        for candidate in pool:
            try:
                own = {c.expression for c in children if c.matches(candidate)}
                excluded = self.manager.excluded_probes(candidate)
                probes = [(key, "gyári", name, expression, "check") for key, (name, expression) in PROBES.items()
                          if key not in excluded and expression not in own]
                pattern_dims = {d for description in candidate.descriptions() for d, _ in with_implied(tuple(sorted(description)))}
                probes += [(key, "gyári", name, expression, "group") for key, (name, expression) in GROUP_PROBES.items()
                           if key not in excluded and expression not in own and GROUP_PROBE_DIMENSIONS.get(key) not in pattern_dims]
                probes += [(c.child_id, "tanított", c.name, c.expression, c.method) for c in children
                           if not c.matches(candidate) and c.expression not in own
                           and not any(t.get("kind") == candidate.kind and t.get("measure") == candidate.measure for t in c.excluded)]
                affected, reference = for_candidate(candidate)
                found = run_probes(probes, affected, reference, timeline_of, levels, as_of.isoformat(), schedules=schedules)
                stored = self.manager.candidate(candidate.candidate_id)
                stored.stats["probes"] = found
                stored.stats["probes_at"] = as_of.isoformat()
                self.manager._save_candidate(stored)
                count += len(found)
            except Exception as error:
                print(f"--> [FELTÁRÁS] {candidate.candidate_id} próbálkozásai nem futottak le: {error}", flush=True)
        return count

    def _filtered_breakdown(self, breakdown_data: Optional[Dict[str, Any]], hidden: List[List[str]], top: int = 6,
                            parent_dims: Iterable[str] = (), measure: str = ""):
        """A „Mi jellemző még?” sorai a tanult preferenciákkal szűrve és rendezve (zsákutca, soha, tiltott út kimarad;
        a mérésnél bevált szempont elöl).

        Hívja: DiscoveryService._refinements."""
        if not breakdown_data:
            return breakdown_data
        prefs = self.manager.breakdown_preferences()
        blocked = self.manager.blocked_breakdown_paths()
        rows = [r for r in breakdown_data.get("rows", [])
                if [r["dimension"], r["value"]] not in hidden and not prefs.get(r["dimension"], {}).get("hidden")
                and not path_blocked(parent_dims, r["dimension"], blocked)]
        rows = [dict(r) for r in rows]
        by_measure = self.manager._breakdown_store()["measures"].get(measure, {}) if measure else {}
        for row in rows:
            row["good"] = by_measure.get(row["dimension"], 0) if measure else prefs.get(row["dimension"], {}).get("good", 0)
            row["label"] = self._name_children(row["label"])   # csak a kiírás: a mezőnév a visszajelzéshez kell
        rows.sort(key=lambda r: (-min(r["good"], 3), -r["difference"]))   # a bevált bontási szempontok előrébb
        return {**breakdown_data, "rows": rows[:top], "hidden_here": len(hidden)}

    def _refinements(self, in_queue: set) -> List[Dict[str, Any]]:
        """A tovább bontott minták a gyerekeikkel együtt: a szülő mérése, a kontextuson belül kiugró
        szűkítések (mennyire kiugróak a szülőhöz képest, és a szülő érintett eseteinek mekkora részét fedik),
        és a megmagyarázatlan maradék.

        Hívja: DiscoveryService.overview."""
        candidates = self.manager.candidates()
        result = []
        for parent in candidates:
            if parent.state != CandidateState.REFINED.value:
                continue
            keys = {pattern_key(d) for d in parent.descriptions()}
            affected = set(parent.bad_incident_ids)
            children, covered = [], set()
            blocked = self.manager.blocked_breakdown_paths()
            parent_dims = {d for d, _ in parent.items}
            for child in candidates:
                if child.stats.get("context") not in keys:
                    continue
                if any(path_blocked(parent_dims, d, blocked) for d, _ in child.items if d not in parent_dims):
                    continue   # tanult zsákutca-úton jött
                own = set(child.bad_incident_ids)
                covered |= own & affected
                s = child.stats
                children.append({
                    "candidate_id": child.candidate_id, "items": child.items, "state": child.state,
                    "in_queue": child.candidate_id in in_queue, "suggestion": child.suggestion,
                    "extra": [list(i) for i in child.items if i not in set(parent.items)],
                    "stats": {k: s.get(k) for k in ("bad_count", "group_size", "rate", "context_rate", "context_lift", "lift")},
                    "coverage": len(own & affected) / len(affected) if affected else None,
                })
            children.sort(key=lambda c: -(c["coverage"] or 0))
            s = parent.stats
            result.append({
                "candidate_id": parent.candidate_id, "measure_label": self._label(parent), "items": parent.items,
                "decided_at": next((d.at for d in reversed(self.manager.decisions())
                                    if d.candidate_id == parent.candidate_id and d.verdict == Verdict.REFINE.value), ""),
                "stats": {k: s.get(k) for k in ("bad_count", "group_size", "rate", "base_rate", "lift")},
                "last_seen_at": parent.last_seen_at, "children": children,
                "method_feedback": parent.feedback.get("methods", {}),
                "breakdown": self._filtered_breakdown(s.get("breakdown"), (parent.stats.get("breakdown_hidden") or []) +
                                                      parent.feedback.get("breakdown_done", []),
                                                      parent_dims={d for d, _ in parent.items}, measure=parent.measure),
                "affected": len(affected), "uncovered": len(affected - covered),
                "kind": parent.kind, "description": s.get("description", ""),
                "trigger_options": trigger_options(parent),
                "child_results": self._name_children(s.get("children") or []),
                "probes": s.get("probes") or [], "probes_at": s.get("probes_at", ""),
            })
        return result

    def _name_children(self, value: Any) -> Any:
        """A szempontként használt almérések nyers dimenzióneve (almeres:CHD-…) helyett a nevük a szövegekben.

        Hívja: DiscoveryService._brief_queue, DiscoveryService._filtered_breakdown,
        DiscoveryService._good_dimension_insights, DiscoveryService._refinements, DiscoveryService.about,
        DiscoveryService.analysis."""
        names = {c.dimension: f"almérés „{c.name}”" for c in self.manager.children()}
        if not names:
            return value

        def rename(item: Any) -> Any:
            if isinstance(item, str):
                for dimension, name in names.items():
                    item = item.replace(dimension, name)
                return item
            if isinstance(item, list):
                return [rename(x) for x in item]
            if isinstance(item, tuple):
                return tuple(rename(x) for x in item)
            if isinstance(item, dict):
                return {k: rename(v) for k, v in item.items()}
            return item

        return rename(value)

    def children_overview(self) -> Dict[str, Any]:
        """Az almérések listája a szókinccsel (az almérés-űrlaphoz és a Tudás fülhöz).

        Hívja: DashboardHandler.do_GET, DiscoveryService.knowledge_overview, felület (GET /api/discovery/children)."""
        with self._lock:
            return {"children": [{**c.to_dict(), "trigger_label": c.triggers_label, "role_label": CHILD_ROLES[c.role],
                                  "method_label": CHILD_METHODS.get(c.method, c.method)}
                                 for c in sorted(self.manager.children(), key=lambda c: c.child_id)],
                    "vocabulary": vocabulary()}

    def children_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """test: kipróbálás egy jelöltön (nem ment); add: mentés és futtatás az illő jelölteken; revoke.

        Hívja: felület (POST /api/discovery/children)."""
        op = payload.get("op")
        if op == "revoke":
            child = self._mutate(lambda: self.manager.revoke_child(str(payload.get("child_id", ""))))
            return {"child": child.to_dict()}
        if op == "confirm":
            child = self._mutate(lambda: self.manager.confirm_child(str(payload.get("child_id", ""))))
            return {"child": child.to_dict()}
        if op == "auto":
            child = self._mutate(lambda: self.manager.set_child_auto(str(payload.get("child_id", "")), bool(payload.get("on"))))
            return {"child": child.to_dict(), "message": f"{child.name}: " + (
                "ha teljesül, „most nem” döntést hoz helyetted (a következő futástól)." if child.auto else "már nem dönt helyetted.")}
        if op == "role":
            child = self._mutate(lambda: self.manager.set_child_role(str(payload.get("child_id", "")), str(payload.get("role", ""))))
            return {"child": child.to_dict(), "message": f"{child.name}: " + (
                "riasztás – ha teljesül, a jelölt a feladat felé tolódik (és ha tovább bontott, újra eléd kerül)."
                if child.role == "alert" else "magyarázat – ha teljesül, a jelölt valószínűleg rendben van.")}
        if op == "dimension":
            child = self._mutate(lambda: self.manager.set_child_dimension(str(payload.get("child_id", "")), bool(payload.get("on"))))
            return {"child": child.to_dict(), "message": f"{child.name}: " + (
                "keresési szempont – a következő feltárástól a minták ez szerint is bonthatók." if child.as_dimension
                else "már nem keresési szempont.")}
        if op == "probe":
            with self.repository.transaction() as db_data:
                with self._lock:
                    candidate_id = str(payload.get("candidate_id", ""))
                    verdict = str(payload.get("verdict", ""))
                    candidate = self.manager.candidate(candidate_id)
                    probe = next((p for p in candidate.stats.get("probes") or [] if p.get("probe") == payload.get("probe")), None)
                    if probe is None:
                        raise ValueError("Ez a próbálkozás már nincs a jelöltön; frissítsd a jelöltsort.")
                    child = self.manager.probe_feedback(candidate_id, probe, verdict)
                    if verdict == "good":
                        self._run_children(db_data, self._now(), [self.manager.candidate(candidate_id)])
                    self._save()
            text = (f"jó módszer – almérés lett belőle ({child.name}), ezentúl minden ilyen jelöltnél magától lefut."
                    if verdict == "good" else "nem jó módszer – ilyen jelöltnél a gép többé nem próbálja.")
            return {"child": child.to_dict() if child else None, "message": f"{probe['name']}: {text}",
                    "effect": self._method_effect(probe["name"], candidate_id, verdict, new_child=verdict == "good")}
        if op == "method":
            with self.repository.transaction() as db_data:
                with self._lock:
                    candidate_id = str(payload.get("candidate_id", ""))
                    verdict = str(payload.get("verdict", ""))
                    child = self.manager.child_method_feedback(str(payload.get("child_id", "")), candidate_id, verdict)
                    pool = [c for c in self.manager.active_candidates() if child.matches(c)]
                    ran = self._run_children(db_data, self._now(), pool) if verdict == "good" else 0
                    self._save()
            kind = self.manager.candidate(candidate_id)
            label = {"signal": "jelzésnél", "contrast": "kiugró mintánál", "trend": "romlásnál"}.get(kind.kind, kind.kind)
            text = (f"jó módszer: ezentúl minden ilyen {label} ({self._key_label(kind.measure)}) magától lefut; most {ran} futás."
                    if verdict == "good" else f"nem jó módszer: ilyen {label} ({self._key_label(kind.measure)}) többé nem fut.")
            return {"child": child.to_dict(), "message": f"{child.name}: {text}",
                    "effect": self._method_effect(child.name, candidate_id, verdict, ran=ran)}
        if op == "attach":
            with self.repository.transaction() as db_data:
                with self._lock:
                    child, added = self.manager.attach_child(str(payload.get("child_id", "")), payload.get("trigger") or {})
                    ran = self._run_children(db_data, self._now(), [c for c in self.manager.active_candidates() if child.matches(c)])
                    self._save()
            text = "hozzárendelve" if added else "ez a hatókör már megvolt"
            return {"child": child.to_dict(), "message": f"{child.name}: {text} ({child.triggers_label}); {ran} futás a döntésre váró jelölteken."}
        expression = rows_to_expression(payload["rows"]) if payload.get("rows") else str(payload.get("expression", ""))
        compile_expression(expression)
        candidate_id = str(payload.get("candidate_id", ""))
        with self.repository.transaction() as db_data:
            with self._lock:
                as_of = self._now()
                if op == "test":
                    candidate = self.manager.candidate(candidate_id)
                    child = ChildDef("PRÓBA", str(payload.get("name") or "Próba"), expression, str(payload.get("role", "explain")),
                                     float(payload.get("threshold", 0.6)), method=str(payload.get("method") or "check"))
                    for_candidate, timeline_of, levels = self._unit_sets(db_data, as_of, {})
                    affected, reference = for_candidate(candidate)
                    schedules = self.knowledge.view(as_of.isoformat()).schedules
                    return {"result": run_child(child, affected, reference, timeline_of, levels, as_of.isoformat(), schedules),
                            "expression": expression}
                if op != "add":
                    raise ValueError("A művelet: test | add | revoke")
                child = self.manager.add_child(str(payload.get("name", "")), expression, str(payload.get("role", "explain")),
                                               float(payload.get("threshold", 0.6)), payload.get("trigger") or {},
                                               str(payload.get("note", "")), source=candidate_id,
                                               as_dimension=bool(payload.get("as_dimension")), auto=bool(payload.get("auto")),
                                               method=str(payload.get("method") or "check"))
                ran = self._run_children(db_data, as_of, [c for c in self.manager.active_candidates() if self.manager.matching_children(c)])
                self._save()
        return {"child": child.to_dict(), "message": f"Almérés mentve ({child.triggers_label}); {ran} futás a döntésre váró jelölteken."}

    def _accept_signal(self, db_data: Dict[str, Any], candidate: Candidate) -> Optional[str]:
        """Elfogadott jelzés -> feladat (issue) a diagnosztika saját mérésével.

        Hívja: DiscoveryService.decide."""
        diagnostic = self.analyzer.diagnostic_registry.get(candidate.measure)
        _, ctx = self.analyzer.review_context(db_data, diagnostic.config_key, self._now())
        run = diagnostic.accept_signal(ctx, candidate.stats.get("subject", ""))
        if not run.measurements:
            return None
        self.analyzer.issue_manager.apply_run(db_data, run, "signal_accepted")
        return run.measurements[0].issue_id

    def _key_label(self, key: str) -> str:
        """Egy mérés vagy egy detektor neve (a jelzés-jelölteknél a detektor kulcsa a mérés).

        Hívja: DiscoveryService._effect, DiscoveryService._label, DiscoveryService._method_effect,
        DiscoveryService._threshold_suggestions, DiscoveryService.breakdown_feedback,
        DiscoveryService.children_action, DiscoveryService.close_issue_with_lesson,
        DiscoveryService.knowledge_overview, DiscoveryService.overview és mások."""
        if key in MEASURES_BY_KEY:
            return _measure_label(key)
        diagnostic = self.analyzer.diagnostic_registry.get(key)
        return (getattr(diagnostic, "signal_label", "") or key) if diagnostic else _measure_label(key)

    def _label(self, candidate: Candidate) -> str:
        """A jelölt mérésének neve (jelzésnél a detektoré).

        Hívja: DiscoveryService._attach_to_open_issues, DiscoveryService._refinements, DiscoveryService._short,
        DiscoveryService._strong_under_rules, DiscoveryService.about, DiscoveryService.candidate_dto."""
        if candidate.kind == "signal":
            diagnostic = self.analyzer.diagnostic_registry.get(candidate.measure)
            return (getattr(diagnostic, "signal_label", "") or candidate.measure) if diagnostic else candidate.measure
        return self._key_label(candidate.measure)

    # ------------------------------------------------------------------
    # Olvasás
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # A döntések hatása: mit változtat a mintakeresésen (a felület felugró ablakban mutatja)
    # ------------------------------------------------------------------

    ENTITY_DIMENSIONS = ("assignment_group", "u_responsibility_unit", "business_service", "location")

    def _waiting_ids(self) -> set:
        """A döntésre váró jelöltek azonosítói (a hatásablak előtte/utána összevetéséhez).

        Hívja: DiscoveryService._effect, DiscoveryService.breakdown_feedback,
        DiscoveryService.close_issue_with_lesson, DiscoveryService.decide, DiscoveryService.rule,
        DiscoveryService.suppress."""
        return {c.candidate_id for c in self.manager._open_pool()}

    def _short(self, candidate_id: str) -> str:
        """Egy jelölt rövid neve a hatásablakba.

        Hívja: DiscoveryService._effect."""
        c = self.manager.candidate(candidate_id)
        key = c.key if len(c.key) <= 70 else c.key[:69] + "…"
        return f"{self._label(c)}: {key}"

    def _effect(self, before: set, title: str, lines: List[str], exclude: Iterable[str] = (), rule_ids: Iterable[str] = (),
                warning: str = "") -> Dict[str, Any]:
        """A döntés előtti és utáni döntésre váró jelöltek különbsége, a létrejött szabályok és a magyarázó sorok.

        Hívja: DiscoveryService._decide_effect, DiscoveryService.breakdown_feedback,
        DiscoveryService.close_issue_with_lesson, DiscoveryService.rule, DiscoveryService.suppress."""
        exclude = set(exclude)
        after = self._waiting_ids()
        hidden = sorted(before - after - exclude)
        returned = sorted(after - before - exclude)
        out = list(lines)
        if hidden:
            out.append(f"A döntésre várók közül még {len(hidden)} jelölt kikerült a sorból: " +
                       "; ".join(self._short(c) for c in hidden[:4]) + (" …" if len(hidden) > 4 else ""))
        if returned:
            out.append(f"{len(returned)} jelölt újra döntésre vár: " + "; ".join(self._short(c) for c in returned[:4]) +
                       (" …" if len(returned) > 4 else ""))
        rules = []
        for rule_id in rule_ids:
            if rule_id and rule_id in self.manager.state["rules"]:
                rule = Rule.from_dict(self.manager.state["rules"][rule_id])
                measures = rule.measure_filter.get("measures") if rule.measure_filter.get("mode") == "list" else None
                rules.append({"rule_id": rule_id, "text": rule.render(),
                              "scope": ("mérés: " + ", ".join(self._key_label(m) for m in measures)) if measures else "minden mérésre",
                              "hard": rule.hard})
        return {"title": title, "lines": out, "rules": rules, "warning": warning, "hidden": len(hidden), "returned": len(returned)}

    def _method_effect(self, name: str, candidate_id: str, verdict: str, ran: Optional[int] = None,
                       new_child: bool = False) -> Dict[str, Any]:
        """A módszer-visszajelzés (jó / nem jó almérés vagy próbálkozás) hatása szavakban.

        Hívja: DiscoveryService.children_action."""
        c = self.manager.candidate(candidate_id)
        kind = {"signal": "jelzésnél", "contrast": "kiugró mintánál", "trend": "romlásnál"}.get(c.kind, c.kind)
        same = sum(1 for other in self.manager._open_pool() if other.kind == c.kind and other.measure == c.measure)
        if verdict == "good":
            lines = [(f"Almérés lett belőle: „{name}”. " if new_child else "") +
                     f"Ezentúl minden {kind} a(z) {self._key_label(c.measure)} mérésnél magától lefut, és az eredménye a kártyán látszik.",
                     f"Most {same} ilyen jelölt vár döntésre" + (f"; {ran} futás történt." if ran is not None else ".")]
            title = "Jó módszer"
        else:
            lines = [f"„{name}” {kind} a(z) {self._key_label(c.measure)} mérésnél többé nem fut (és a gép sem próbálja).",
                     "A Tudás › Almérések alatt visszaállítható."]
            title = "Nem jó módszer"
        return {"title": title, "lines": lines, "rules": [], "warning": "", "hidden": 0, "returned": 0}

    def _decide_effect(self, action: str, candidate_id: str, before: set, result: Mapping[str, Any]) -> Dict[str, Any]:
        """Egy döntés (feladat, most nem, bontás) hatása a mintakeresésre szavakban.

        Hívja: DiscoveryService.decide."""
        c = self.manager.candidate(candidate_id)
        if action == "finding":
            return self._effect(before, "Feladat: issue nyílt", [
                f"Issue: {result.get('issue_id') or '-'}.",
                "A gép ebből még nem tanul: a lezáráskor adott tanulságból fog (valódi hiba → a tipp erős címkéje; nem hiba → "
                "szabály; munkaidőn kívül → tény; ismert eltérés → saját norma; más küszöb → szűkebb küszöb).",
                "Amíg az issue nyitott, a mintájára illő új jelöltek hozzá csatolódnak, nem kerülnek eléd."], exclude=[candidate_id])
        if action == "reject_once":
            lines = ["A jelölt félrekerült. Ha új érintett esettel visszajön, újra eléd kerül.",
                     f"Eddig {c.not_now}-szer mondtad rá: most nem. Visszatérve ennyivel kerül előrébb: "
                     f"+{self.manager.not_now_boost * c.not_now:.0f} prioritás.",
                     "A tipp gyenge ellenpéldaként tanul belőle: a hasonló jelöltekre kicsit kevésbé mond „feladat”-ot."]
            if c.not_now >= 3:
                lines.append("Mindig visszajön: érdemes feladatot nyitni rá, vagy a jelölt oldaláról elnyomni (Ez a minta soha).")
            return self._effect(before, "Most nem", lines, exclude=[candidate_id])
        if action == "refine":
            lines = (["A minta kontextus lett: maga nem jön vissza, a mintakeresés ezen belül keresi, ami hozzá képest is kiugrik."]
                     if c.kind == "contrast" else ["A jelölt vizsgálat alatt áll: a sorból kikerült, a munkalapján dolgozhatsz."])
            lines.append(f"A gép {result.get('probes', 0)} próbálkozása tér el érdemben az érintetteknél; "
                         f"{len(c.stats.get('children') or [])} almérés futott rajta.")
            lines.append("Döntést (Feladat / Most nem) a jelölt oldalán hozhatsz, amikor megvan, mi ez.")
            return self._effect(before, "Bontsd tovább", lines, exclude=[candidate_id])
        return self._effect(before, "Döntés rögzítve", [str(result.get("message", ""))], exclude=[candidate_id],
                            rule_ids=[result.get("rule_id")])

    @staticmethod
    def _dim_label(dim: str) -> str:
        """Egy mező neve a felületen (a keresési szerep-címke, különben a bontási címke). Hívja: about, suppress,
        breakdown_feedback."""
        return DIMENSION_ROLE_LABELS.get(dim) or DIMENSION_LABELS.get(dim, dim)

    def about(self, c: Candidate) -> Dict[str, Any]:
        """Miről szól a jelölt: a mérés (mi számít érintettnek, mire vonatkozik) és a minta feltételei szavakban.

        Hívja: DiscoveryService.candidate_dto."""
        if c.kind == "signal":
            return {"measure_label": self._label(c), "description": str(c.stats.get("description", "")), "signal": True,
                    "conditions": [{"dim": d, "value": v, "label": self._dim_label(d),
                                    "text": f"{self._dim_label(d)}: {v}",
                                    "field": d in DIMENSION_ROLE_DEFAULTS} for d, v in c.items if d not in ("subject", "incident")]}
        measure = MEASURES_BY_KEY.get(c.measure)
        scope = measure.scope if measure else "ticket"
        return {
            "measure_label": measure.label if measure else c.measure, "description": measure.description if measure else "",
            "threshold": self.config.measure_threshold(c.measure) if measure else None, "unit": measure.unit if measure else "",
            "kind": measure.kind if measure else "", "scope": scope,
            "scope_text": ("egy csoportnál töltött szakasz (a jegy egy csoporthoz kerülésétől a továbbításáig vagy lezárásáig)"
                           if scope == "stint" else "a ticket egésze"),
            "conditions": [{"dim": d, "value": v, "label": self._dim_label(d),
                            "value_text": _value_text(d, v), "text": self._name_children(describe_condition(d, v, scope)),
                            "field": d in DIMENSION_ROLE_DEFAULTS} for d, v in c.items],
        }

    def suppress(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Elnyomás a jelölt oldaláról. pattern: ez a minta ennél a mérésnél soha (szabály); element: a mintaelem
        (mező = érték) egyik mérésnél se; dimension: a mező egyik értéke se, és a mintakeresés sem használja (szerep 0).
        Mind szabály, a Tudás fülön visszavonható (a mező szerepe a visszavonáskor visszaáll).

        Hívja: felület (POST /api/discovery/suppress)."""
        op, candidate_id = str(payload.get("op", "")), str(payload.get("candidate_id", ""))
        dim, value = str(payload.get("dim", "")), str(payload.get("value", ""))
        note = str(payload.get("note", "")).strip()
        with self._lock:
            if candidate_id not in self.manager.state["candidates"]:
                raise ValueError("Nincs ilyen jelölt; frissítsd a jelöltsort.")
            candidate = self.manager.candidate(candidate_id)
        label = self._dim_label(dim)
        with self._lock:
            before = self._waiting_ids()
        result = self._suppress(op, candidate_id, candidate, dim, value, note, label)
        lines = {"pattern": ["Ez a pontos minta ennél a mérésnél többé nem jön, a nagyon erős (áttörő) változata sem."],
                 "element": [f"Egyik mérésnél sem jön többé olyan minta, amelyben „{label} = {value}” szerepel."],
                 "dimension": [f"A(z) {label} mező egyik értéke sem jön többé, és a mintakeresés sem bont szerinte (szerepe 0)."]}[op]
        lines.append("Szabály: a Tudás › Szabályok alatt visszavonható" + (" (a mező szerepe is visszaáll)." if op == "dimension" else "."))
        warning = ""
        if op in ("element", "dimension") and dim in self.ENTITY_DIMENSIONS:
            warning = (f"A(z) {label} egy csoport / egység / service: az elnyomás a jövőbeli valódi hibáit is elrejti minden mérésnél. "
                       f"Ha csak ez a minta zaj, az „Ez a minta soha” a kisebb lépés.")
        with self._lock:
            result["effect"] = self._effect(before, {"pattern": "Ez a minta soha", "element": "Ezt az elemet soha",
                                                     "dimension": f"{label}: az egész mezőt soha"}[op],
                                            lines, exclude=[candidate_id], rule_ids=[result.get("rule_id")], warning=warning)
        return result

    def _suppress(self, op: str, candidate_id: str, candidate: Candidate, dim: str, value: str, note: str, label: str) -> Dict[str, Any]:
        """Az elnyomás végrehajtása: a pontos minta, egy elem vagy egy egész mező kemény szabálya.

        Hívja: DiscoveryService.suppress."""
        if op == "pattern":
            def apply_pattern() -> Any:
                decision = self.manager.decide(candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value, note=note or "elvetett minta")
                rule = Rule.from_dict(self.manager.state["rules"][decision.rule_id])
                rule.hard = True   # kifejezett elnyomás: az áttörő erősségű szűkítést is rejti
                self.manager._save_rule(rule)
                self.manager.apply_rules(count_hits=False)
                return rule
            rule = self._mutate(apply_pattern)
            return {"rule_id": rule.rule_id, "message": f"Ez a minta ennél a mérésnél többé nem jön (szabály {rule.rule_id})."}
        if op == "element":
            if not dim or not value:
                raise ValueError("Hiányzik a mintaelem.")
            rule = self._mutate(lambda: self.manager.add_rule([{"dim": dim, "values": [value]}], reason=note or f"elnyomott elem: {label} = {value}",
                                                              source=f"element:{candidate_id}", hard=True))
            return {"rule_id": rule.rule_id, "message": f"{label} = {value}: egyik mérésnél sem jön többé olyan minta, amelyben szerepel (szabály {rule.rule_id})."}
        if op == "dimension":
            if dim not in DIMENSION_ROLE_DEFAULTS:
                raise ValueError(f"A(z) {dim} mező nem kapcsolható ki a keresésből.")
            def apply() -> Any:
                self.config.update(DIMENSIONS_SECTION, dim, 0)
                return self.manager.add_rule([{"dim": dim, "values": ["*"]}], reason=note or f"a(z) {label} mező kikapcsolva",
                                             source=f"dimension:{dim}", hard=True)
            rule = self._mutate(apply)
            return {"rule_id": rule.rule_id, "message": f"{label}: a mező egyik értéke sem jön többé, és a mintakeresés sem bont szerinte "
                                                        f"(szabály {rule.rule_id}; visszavonás: Tudás › Szabályok)."}
        raise ValueError("A művelet: pattern | element | dimension")

    def candidate_dto(self, c: Candidate, full: bool = False) -> Dict[str, Any]:
        """A jelölt a felületnek (a „Miről szól?” adataival, a próbálkozásokkal, a hatókörrel); teljes nézetben az
        érintett jegyekkel és a darabszámokkal.

        Hívja: DiscoveryService.analysis, DiscoveryService.backlog_calendar, DiscoveryService.candidate,
        DiscoveryService.overview."""
        dto = {
            "candidate_id": c.candidate_id, "measure": c.measure, "measure_label": self._label(c),
            "scope": MEASURES_BY_KEY[c.measure].scope if c.measure in MEASURES_BY_KEY else "ticket",
            "key": c.key, "items": [list(item) for item in c.items], "alternatives": c.alternatives,
            "state": c.state, "stats": c.stats, "flags": c.flags, "suggestion": c.suggestion,
            "priority": c.priority, "covered_by": c.covered_by, "baseline": c.baseline,
            "filtered_by": c.filtered_by, "boosted_by": c.boosted_by, "issue_id": c.issue_id,
            "first_seen_at": c.first_seen_at, "last_seen_at": c.last_seen_at, "occurrences": len(c.occurrences),
            "reach": len(c.reach), "kind": c.kind, "trigger_options": trigger_options(c), "not_now": c.not_now,
            "about": self.about(c),
            "baseline_progress": self.manager.baseline_progress(c),
        }
        if full:
            dto.update({"bad_incident_ids": c.bad_incident_ids, "counts": c.counts[-12:], "occurrence_log": c.occurrences[-12:],
                        "window_start": c.window_start, "window_end": c.window_end})
        return dto

    def overview(self, show_all: bool = False) -> Dict[str, Any]:
        """A Jelöltsor adatai: a mai keret szerinti jelöltek, családok (csomópontok, bontások), kérdések, ismert
        eltérések, állapotok, döntésnapló és tanulási mérleg.

        Hívja: DashboardHandler.do_GET, felület (GET /api/discovery)."""
        with self._lock:
            configure(self.manager, self.config)
            capacity = self.manager.capacity()
            limit = self.config.get("queue", "daily_limit")
            # a jelöltsor a mai megmaradt döntési keretig mutat (kérésre a többit is)
            queue = self.manager.queue(limit, None if show_all else capacity["remaining"])
            waiting = len(self.manager.queue(10_000))
            states: Dict[str, int] = {}
            for candidate in self.manager.candidates():
                states[candidate.state] = states.get(candidate.state, 0) + 1
            baselined = [self.candidate_dto(c) for c in self.manager.candidates() if c.state == CandidateState.BASELINED.value]
            decisions = self.manager.decisions()[-30:]
            by_id = {c.candidate_id: c for c in self.manager.candidates()}
            in_queue = {c.candidate_id for c in queue}

            def member(candidate_id: str) -> Dict[str, Any]:
                c = by_id[candidate_id]
                s = c.stats
                return {"candidate_id": candidate_id, "key": c.key, "items": c.items, "measure_label": self._key_label(c.measure),
                        "kind": c.kind, "in_queue": candidate_id in in_queue, "suggestion": c.suggestion,
                        "stats": {k: s.get(k) for k in ("bad_count", "group_size", "rate", "base_rate", "lift", "description")}}

            keys = {c.candidate_id: (c.key, self._key_label(c.measure)) for c in self.manager.candidates()}
            clusters = [{**cluster, "member_keys": [member(m)
                                                    for m in cluster["members"]]}
                        for cluster in self.manager.clusters()]
            return {
                "now": self.manager.now,
                "last_run": self.manager.state["meta"].get("last_run"),
                "queue": [self.candidate_dto(c) for c in queue],
                "clusters": clusters,
                "capacity": capacity,
                "waiting": waiting,
                "show_all": show_all,
                # a megválaszolandó elérhetőségi kérdések (a kereten kívül is: ezek tényt adnak, nem feladatot)
                "questions": [self.candidate_dto(c) for c in self.manager.questions()],
                "child_names": {c.child_id: c.name for c in self.manager.children()},
                "attached_count": sum(1 for c in self.manager.candidates() if c.state == CandidateState.ATTACHED.value),
                "refinements": self._refinements({c.candidate_id for c in queue}),
                "balance": self.learning_balance(),
                "dimension_labels": {**{d: l[:1].upper() + l[1:] for d, l in DIMENSION_LABELS.items()}, **DIMENSION_ROLE_LABELS,
                                     "feature": "Funkció", "subject": "Tárgy"},
                "baselined": baselined,
                "states": states,
                "learning": self.manager.learning_stats(),
                "decisions": [{**d.to_dict(), "key": self.manager.candidate(d.candidate_id).key,
                               "measure_label": self._key_label(self.manager.candidate(d.candidate_id).measure)}
                              for d in reversed(decisions) if d.candidate_id in self.manager.state["candidates"]],
            }

    def candidate(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """Egy jelölt teljes adatai (a jelölt oldalához).

        Hívja: DashboardHandler.do_GET, felület (GET /api/discovery/candidates/)."""
        with self._lock:
            if candidate_id not in self.manager.state["candidates"]:
                return None
            return self.candidate_dto(self.manager.candidate(candidate_id), full=True)

    def analysis(self, candidate_id: str) -> Optional[Dict[str, Any]]:
        """A jelölt elemzése döntéshez (magyarázat, közös jellemzők, egységek, trend, eloszlás).

        Hívja: DashboardHandler.do_GET, felület (GET /api/discovery/candidates/<id>/analysis)."""
        incidents = self._incidents()
        with self._lock:
            if candidate_id not in self.manager.state["candidates"]:
                return None
            candidate = self.manager.candidate(candidate_id)
            as_of = datetime.fromisoformat(candidate.window_end) if candidate.window_end else self._now(incidents)
            view = self.knowledge.view(as_of.isoformat())
            lookback = self.config.get("queue", "lookback_days")
            threshold = self.config.measure_threshold(candidate.measure)
        result = analyze_candidate(candidate, incidents, as_of, lookback, threshold, view.levels, derived=view.derived,
                                   schedules=view.schedules)
        # csak a megjelenített szövegek: a mezőnevek a felületen szűréshez is kellenek
        for key in ("conditions", "summary"):
            result[key] = self._name_children(result[key])
        result["verdict"]["text"] = self._name_children(result["verdict"]["text"])
        result["candidate"] = self.candidate_dto(candidate, full=True)
        data_end = self.analyzer._latest_event_time(incidents) if incidents else None
        result["data_end"] = data_end.isoformat() if data_end else None
        # a jelölt egy olyan mérésből való, amelyet a mostani adat nem fed le (pl. reset vagy fut a lejátszás)
        result["stale"] = bool(data_end is None or data_end < as_of - timedelta(hours=1))
        return result

    def knowledge_overview(self) -> Dict[str, Any]:
        """A Tudás fül adatai: tények, szabályok, almérések, tanulságok, küszöbök, bontási szempontok, mentések.

        Hívja: DashboardHandler.do_GET, felület (GET /api/discovery/knowledge)."""
        with self._lock:
            facts = sorted(self.knowledge.facts(), key=lambda f: f.fact_id)
            rules = sorted(self.manager.rules(), key=lambda r: r.rule_id)
            covered: Dict[str, int] = {}
            for candidate in self.manager.candidates():
                rule_id = (candidate.filtered_by or {}).get("rule_id")
                if rule_id:
                    covered[rule_id] = covered.get(rule_id, 0) + 1
            return {
                "facts": [{**f.to_dict(), "rendered": f.render(), "active": f.is_active(self.manager.now)} for f in facts],
                "rules": [{**r.to_dict(), "rendered": r.render(), "covers": covered.get(r.rule_id, 0)} for r in rules],
                "predicates": [{"name": p.name, "description": p.description, "kind": p.kind,
                                "args": [{"name": arg, "dimension": dim} for arg, dim in p.args]}
                               for p in PREDICATES.values()],
                "levels": dict(self.manager.knowledge_view().levels),
                **self.children_overview(),
                "lessons": list(reversed(self.knowledge.store.get("lessons", [])[-50:])),
                "threshold_overrides": [{**o.to_dict(), "label": o.label()} for o in self.knowledge.threshold_overrides()
                                        if o.state == "active"],
                "backups": self.backup_overview(),
                "breakdown_dimensions": [{"dimension": d, **p, "measures": [
                    f"{self._key_label(m)} ({counts[d]}×)" for m, counts in sorted(self.manager._breakdown_store()["measures"].items()) if d in counts]}
                    for d, p in sorted(self.manager.breakdown_preferences().items())],
                "breakdown_paths": [{"parent_class": a, "dimension": b} for a, b in sorted(self.manager.blocked_breakdown_paths())],
                "contexts": [{"candidate_id": c.candidate_id, "measure_label": self._key_label(c.measure), "items": c.items,
                              "decided_at": next((d.at for d in reversed(self.manager.decisions())
                                                  if d.candidate_id == c.candidate_id and d.verdict == Verdict.REFINE.value), ""),
                              "children": sum(1 for o in self.manager.candidates()
                                              if o.stats.get("context") in {pattern_key(x) for x in c.descriptions()})}
                             for c in self.manager.candidates() if c.state == CandidateState.REFINED.value],
            }

    def breakdown_feedback(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Bontási visszajelzés. "good": jó szempont – ennél a mérésnél a gép ezt a mezőt minden jelöltnél megnézi (a
        kártyán „Bevált szempontok”), és a bontásokban előrébb kerül; az érték nem lesz jelölt. "dead_end": ez a sor
        ennél a bontásnál nem jön; "never": a mező egyik bontásban sem.

        Hívja: felület (POST /api/discovery/breakdown)."""
        verdict = str(payload.get("verdict", ""))
        if verdict == "reset":
            self._mutate(lambda: self.manager.reset_breakdown_dimension(str(payload.get("dimension", ""))))
            return {"message": "A bontási szempont alapállapotba állt."}
        if verdict == "unblock_path":
            self._mutate(lambda: self.manager.block_breakdown_path(str(payload.get("parent_class", "")),
                                                                   str(payload.get("dimension", "")), blocked=False))
            return {"message": "A bontási út újra engedélyezett."}
        parent_id, dimension, value = str(payload.get("candidate_id", "")), str(payload.get("dimension", "")), str(payload.get("value", ""))
        with self.repository.transaction() as db_data:
            with self._lock:
                before = self._waiting_ids()
                self.manager.breakdown_feedback(parent_id, dimension, value, verdict)
                result: Dict[str, Any] = {"verdict": verdict}
                if verdict == "good":
                    measure = self.manager.candidate(parent_id).measure
                    same = [c for c in self.manager.active_candidates() if c.measure == measure]
                    as_of = self._now()
                    result["updated"] = self._good_dimension_insights(same, self._profiles(self._incidents(db_data), as_of), as_of)
                    result["message"] = "Jó szempont: ennél a mérésnél a gép ezentúl minden jelöltnél megnézi."
                elif verdict == "dead_end":
                    result["message"] = "Ennél a bontásnál ez a sor többé nem jelenik meg."
                    if any(s["key"] == f"breakdown_never|{dimension}" or (s["kind"] == "breakdown_path" and s["dimension"] == dimension)
                           for s in self._generalizations()):
                        result["message"] += " Ezt a bontást már több hasonló szülőnél zsákutcának jelölted: a Javaslatok fülön javaslat vár."
                else:
                    result["message"] = "Ez a mező egyik bontásban sem jelenik meg többé (a Tudás fülön visszaállítható)."
                self._save()
                label = self._dim_label(dimension)
                measure_label = self._key_label(self.manager.candidate(parent_id).measure)
                lines = {"good": [f"Jó szempont: a gép ezentúl minden „{measure_label}” jelöltnél megnézi a(z) {label} mezőt, és a kártyán "
                                  f"„Bevált szempontok” sorban mutatja, mi jellemző rá (most {result.get('updated', 0)} jelöltön).",
                                  f"A bontási táblákban ennél a mérésnél a(z) {label} előrébb kerül.",
                                  f"A sor értéke („{value}”) nem lett jelölt: ez a visszajelzés a módszerről szól, nem az értékről."],
                         "dead_end": [f"Ennél a szülőnél a „{label} = {value}” sor többé nem jön.",
                                      "Ha több hasonló szülőnél is zsákutca, a Javaslatok fülön általános javaslat jön (ez a bontási út soha)."],
                         "never": [f"A(z) {label} mező egyik bontásban sem jelenik meg többé.",
                                   "A mintakeresést nem érinti, csak a „Mi jellemző még?” listát; a Tudás fülön visszaállítható."]}.get(verdict, [])
                result["effect"] = self._effect(before, {"good": "Jó szempont", "dead_end": "Zsákutca", "never": "Ezt a mezőt soha (bontásban)"}.get(verdict, "Bontás"),
                                                lines)
        return result

    def _schedule_suggestions(self, timelines, now: datetime, view, weeks: int = 8) -> List[Dict[str, Any]]:
        """Munkaidő-javaslatok csoportonként a tényleges aktivitásból (ahol nincs saját munkaidő).

        Hívja: DiscoveryService.review."""
        counts = working_activity(timelines, now - timedelta(weeks=weeks), now)
        dismissed = self.knowledge.store.get("schedule_dismissed", {})
        closing: Dict[str, List[Any]] = {}
        for timeline in timelines:
            closed = timeline.first_closed_at
            stint = next((s for s in reversed(timeline.stints) if s.end_reason == "closed"), None)
            if closed and stint is not None:
                closing.setdefault(stint.group, []).append(timeline)
        result = []
        for group, counter in counts.items():
            if group in view.schedules:
                continue
            suggestion = infer_schedule(counter, coverage=self.config.schedule_coverage())
            if suggestion is None or dismissed.get(group) == suggestion.schedule.label():
                continue
            pairs = []
            for timeline in closing.get(group, []):
                snow = timeline.value_at("business_stc")
                try:
                    snow_seconds = float(snow)
                except (TypeError, ValueError):
                    continue
                if snow_seconds > 0:
                    pairs.append((suggestion.schedule.working_seconds(timeline.opened_at, timeline.first_closed_at), snow_seconds))
            schedule = suggestion.schedule
            result.append({"group": group, "label": schedule.label(), "days": sorted(schedule.days), "start": schedule.start,
                           "end": schedule.end, "actions": suggestion.actions, "coverage": suggestion.coverage,
                           "weekend_share": suggestion.weekend_share, "servicenow": compare_with_servicenow(pairs),
                           "level": view.levels.get(group, "")})
        result.sort(key=lambda r: -r["actions"])
        return result

    def schedule_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Munkaidő-javaslat: accept (a megadott napokkal és órákkal; ha módosítottad, kézi forrás) | dismiss.

        Hívja: felület (POST /api/discovery/schedule)."""
        op, group = str(payload.get("op", "")), str(payload.get("group", "")).strip()
        if not group:
            raise ValueError("Hiányzik a csoport.")
        if op == "dismiss":
            self.knowledge.store.setdefault("schedule_dismissed", {})[group] = str(payload.get("label", ""))
            self.knowledge.save()
            return {"message": "Rendben, ezt a munkaidő-javaslatot nem kéred; ha az aktivitás érdemben változik, új javaslat jön."}
        if op != "accept":
            raise ValueError("A művelet: accept | dismiss")
        days = frozenset(int(d) for d in payload.get("days") or [])
        schedule = Schedule(days, int(payload.get("start", 0)), int(payload.get("end", 24)))
        if not days or not (0 <= schedule.start <= 23) or not (1 <= schedule.end <= 24):
            raise ValueError("Adj meg legalább egy napot, és órát 0–24 között.")
        source = "tanult (aktivitásból, jóváhagyva)" if payload.get("learned") else "kézi"

        def apply() -> Any:
            for fact in self.knowledge.facts():   # a csoport korábbi munkaideje helyett
                if fact.predicate == "munkaido" and fact.state == "active" and fact.args.get("csoport") == group:
                    self.manager.revoke_fact(fact.fact_id)
            return self.manager.add_fact("munkaido", working_fact_args(group, schedule), note=source)

        fact = self._mutate(apply)
        return {"fact_id": fact.fact_id, "message": f"Munkaidő rögzítve: {schedule.label()}. A felvételi és a hold idő ennél a "
                                                   f"csoportnál a következő feltárástól munkaórában számít."}

    # ------------------------------------------------------------------
    # Backlog-naptár
    # ------------------------------------------------------------------

    def backlog_calendar(self, group: str = "", days: int = 28) -> Dict[str, Any]:
        """Egy csoport várakozó jegyei napok × órák szerint, a munkaidejével és a rá talált mintákkal.

        Hívja: DashboardHandler.do_GET, felület (GET /api/discovery/backlog)."""
        incidents = self._incidents()
        with self._lock:
            now = self._now(incidents)
            view = self.knowledge.view(now.isoformat())
            since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            timelines = [t for i in incidents if (t := incident_timeline(i, now)) is not None]
            by_id = {t.incident_id: t for t in timelines}
            relevant = [c for c in self.manager.candidates()
                        if c.state in (CandidateState.QUEUED.value, CandidateState.REFINED.value,
                                       CandidateState.BASELINED.value) and c.kind != "signal"]
            # a csoportválasztó: ahol jelölt van, előre; utána a legforgalmasabbak
            counts: Dict[str, int] = {}
            for c in relevant:
                for g in backlog_candidate_groups(c):
                    counts[g] = counts.get(g, 0) + 1
            activity: Dict[str, int] = {}
            for t in timelines:
                for s in t.stints:
                    if s.start >= since:
                        activity[s.group] = activity.get(s.group, 0) + 1
            groups = sorted(activity, key=lambda g: (-counts.get(g, 0), -activity[g]))[:60]
            group = group or (groups[0] if groups else "")
            stints = backlog_group_stints(timelines, group, since, now)
            # ne mutasson üres napokat az adat kezdete előtt
            first = min((t.opened_at for t in timelines), default=since)
            if first > since:
                since = first.replace(hour=0, minute=0, second=0, microsecond=0)
                days = max(1, (now.date() - since.date()).days + 1)
            cells = backlog_cells(stints, since, days)
            items = []
            for c in relevant:
                scope = MEASURES_BY_KEY[c.measure].scope if c.measure in MEASURES_BY_KEY else "ticket"
                on_group = group in backlog_candidate_groups(c)
                arrivals = backlog_affected(c, stints, scope, since, by_id)
                if not on_group and not arrivals:
                    continue
                dto = self.candidate_dto(c)
                items.append({**dto, "on_group": on_group, "marks": backlog_time_marks(c.items), "arrivals": arrivals})
            items.sort(key=lambda c: (not c["on_group"], c["state"] != "queued", -len(c["arrivals"])))
            schedule = view.schedules.get(group)
            return {
                "group": group, "now": now.isoformat(), "since": since.isoformat(), "days": days,
                "groups": [{"group": g, "candidates": counts.get(g, 0), "stints": activity[g], "level": view.levels.get(g, "")} for g in groups],
                "cells": cells, "candidates": items[:40],
                "schedule": {"days": sorted(schedule.days), "start": schedule.start, "end": schedule.end, "label": schedule.label(),
                             "source": schedule.source} if schedule else None,
                "level": view.levels.get(group, ""),
            }

    def _generalizations(self) -> List[Dict[str, Any]]:
        """Az általánosítási javaslatok (a már elutasítottak nélkül).

        Hívja: DiscoveryService.breakdown_feedback, DiscoveryService.generalize, DiscoveryService.review."""
        from learning.config import DIMENSION_ROLE_LABELS
        from learning.analysis import DIMENSION_LABELS
        store = self.knowledge.store
        return generalization_suggestions(
            self.manager.rules(), store.get("breakdown", {}), self.config.dimension_roles(),
            store.get("generalization_dismissed", {}), self.config.generalize_min_count(),
            {**DIMENSION_LABELS, **DIMENSION_ROLE_LABELS, **{c.dimension: f"almérés „{c.name}”" for c in self.manager.children()}})

    def generalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Általánosítási javaslat elfogadása vagy elutasítása (az elutasítás a bizonyíték mostani szintjén jegyződik).

        Hívja: felület (POST /api/discovery/generalize)."""
        key, op = str(payload.get("key", "")), str(payload.get("op", ""))
        with self._lock:
            item = next((s for s in self._generalizations() if s["key"] == key), None)
            if item is None:
                raise ValueError("Ez a javaslat már nem aktuális (frissítsd a Javaslatok fület).")
            if op == "dismiss":
                self.knowledge.store.setdefault("generalization_dismissed", {})[key] = item["evidence"]
                self.knowledge.save()
                return {"message": "Rendben, nem kéred. Csak akkor jön vissza, ha érdemben több bizonyíték gyűlik mögé."}
            if op != "accept":
                raise ValueError("A művelet: accept | dismiss")
            kind = item["kind"]
            if kind == "breakdown_path":
                self.manager.block_breakdown_path(item["parent_class"], item["dimension"])
                message = "Megjegyeztem a bontási utat: ilyen szülőn belül ez a bontás nem jön (Tudás › Bontási szempontok)."
            elif kind == "breakdown_never":
                prefs = self.manager._breakdown_store()["dimensions"].setdefault(item["dimension"], {"good": 0, "hidden": False})
                prefs["hidden"] = True
                self.knowledge.save()
                message = "A mező egyik bontásban sem jelenik meg többé (Tudás › Bontási szempontok)."
            elif kind in ("emphasize", "dimension_role"):
                role = 3 if kind == "emphasize" else int(item["role"])
                self.config.update("dimensions", item["dimension"], role)
                configure(self.manager, self.config)
                message = f"A mező szerepe a keresésben: {role} (Paraméterek › Mezők szerepe); a következő feltárástól érvényes."
            elif kind == "general_rule":
                rule = self.manager.add_rule([{"dim": item["dimension"], "values": [item["value"]]}],
                                             measures=item["measures"] or None,
                                             reason=f"általánosítás: {', '.join(item['rules'])}", source="generalization")
                message = f"Általános szabály: {rule.rule_id} (Tudás › Szabályok; visszavonható)."
            else:
                raise ValueError(f"Ismeretlen javaslat: {kind}")
            self._save()
        return {"message": message}

    # ------------------------------------------------------------------
    # Lezárás tanulsággal (bármely issue)
    # ------------------------------------------------------------------

    def _issue_context(self, db_data: Dict[str, Any], issue_id: str):
        """Egy issue, a belőle nyílt jelölt, a jelzés leírása és a detektor (a lezáráshoz).

        Hívja: DiscoveryService.close_issue_with_lesson, DiscoveryService.lesson_options."""
        data = (db_data.get("issues") or {}).get(issue_id)
        if data is None:
            raise ValueError(f"Nincs ilyen issue: {issue_id}")
        issue = data if isinstance(data, dict) else data.to_dict()
        details = issue.get("details") or {}
        candidate_id = details.get("candidate_id") or next(
            (c.candidate_id for c in self.manager.candidates() if c.issue_id == issue_id), None)
        candidate = self.manager.candidate(candidate_id) if candidate_id in self.manager.state["candidates"] else None
        diagnostic_key = issue.get("diagnostic_key") or issue.get("category", "")
        diagnostic = self.analyzer.diagnostic_registry.get(diagnostic_key)
        signal_items = diagnostic.signal_items(issue.get("subject", ""), details) if diagnostic is not None else ()
        if signal_items and list(signal_items) == [("subject", issue.get("subject", ""))]:
            signal_items = ()   # a diagnosztika nem ad értelmes leírást: szabály nem képezhető belőle
        return issue, candidate, signal_items, diagnostic_key

    def _threshold_context(self, db_data: Dict[str, Any], issue: Mapping[str, Any], diagnostic_key: str):
        """A detektor küszöbei (a mérésnél érvényes értékkel) és a mért eset entitásai a küszöb-tanulsághoz.

        Hívja: DiscoveryService._check_follow_ups, DiscoveryService.close_issue_with_lesson,
        DiscoveryService.lesson_options."""
        diagnostic = self.analyzer.diagnostic_registry.get(diagnostic_key)
        if diagnostic is None or diagnostic_key == "PATTERN_FINDING":
            return [], []
        details = dict(issue.get("details") or {})
        used = {**((issue.get("params_snapshot") or {}).get("thresholds") or {}), **(details.get("thresholds_used") or {})}
        defs = [{"key": d.key, "label": d.label, "unit": d.unit, "value": used.get(d.key, d.default)}
                for d in diagnostic.threshold_definitions()]
        if getattr(diagnostic, "family", "") == "SINGLE_TICKET" and not details.get("entity"):
            incident = next((i for i in self._incidents(db_data) if str(i.id) == issue.get("subject")), None)
            if incident is not None:
                timeline = incident_timeline(incident, self._now())
                if timeline is not None:
                    details["entity"] = {"assignment_group": list(timeline.groups), "current_group": timeline.value_at("assignment_group"),
                                         "business_service": timeline.value_at("business_service")}
        return defs, diagnostic.entity_items(issue.get("subject", ""), details)

    # ------------------------------------------------------------------
    # Hozzácsapás és ismert hiba-fajta (őrök a feladatokra)
    # ------------------------------------------------------------------

    OPEN_ISSUE_STATES = {"active", "not_relevant", "outdated"}

    def _attach_to_open_issues(self, db_data: Dict[str, Any], now: datetime) -> List[Tuple[str, str]]:
        """A nyitott issue mintájára illő új jelöltek az issue-hoz kerülnek (új érintett esetekként), nem a sorba.

        Hívja: DiscoveryService._run."""
        issues = db_data.get("issues") or {}
        state_of = lambda data: (data if isinstance(data, dict) else data.to_dict()).get("state")
        open_ids = {iid for iid, data in issues.items() if state_of(data) in self.OPEN_ISSUE_STATES}
        self.manager.release_attached(open_ids)
        attached = self.manager.attach_to_open_issues(open_ids)
        for candidate_id, issue_id in attached:
            candidate = self.manager.candidate(candidate_id)
            entries = {e["candidate_id"]: e for e in self._issue_details(issues[issue_id]).get("attached", [])}
            previous = entries.get(candidate_id, {})
            entries[candidate_id] = {
                "candidate_id": candidate_id, "key": candidate.key, "measure_label": self._label(candidate),
                "bad_count": candidate.stats.get("bad_count"), "group_size": candidate.stats.get("group_size"),
                "new_cases": sorted(set(candidate.bad_incident_ids) - set(previous.get("incidents", []))),
                "incidents": candidate.bad_incident_ids, "first_at": previous.get("first_at", now.isoformat()), "last_at": now.isoformat()}
            self.analyzer.issue_manager.update_details(db_data, issue_id, {
                "attached": list(entries.values()),
                "attached_news": {"at": now.isoformat(), "count": sum(len(e["new_cases"]) for e in entries.values())}})
        if attached:
            self.manager.refresh_suggestions()
        return attached

    @staticmethod
    def _issue_details(stored: Any) -> Dict[str, Any]:
        """Egy tárolt issue details mezője olvasásra (Issue objektum vagy szótár).

        Hívja: DiscoveryService._attach_to_open_issues."""
        return (stored.details if hasattr(stored, "details") else stored.get("details")) or {}

    def _check_known_faults(self, db_data: Dict[str, Any], now: datetime) -> List[str]:
        """„Valódi hiba” tanulsággal lezárt issue: ha a mérése elcsendesedett, majd újra túllépi a küszöböt, a hiba
        visszatért – az issue újranyílik (ismert hiba-fajta: nem jelöltként kerül újra eléd).

        Hívja: DiscoveryService._run."""
        reopened = []
        for issue_id, stored in (db_data.get("issues") or {}).items():
            data = stored if isinstance(stored, dict) else stored.to_dict()
            details = data.get("details") or {}
            if data.get("state") != "resolved" or (details.get("lesson") or {}).get("outcome") != "confirmed":
                continue
            exceeded = details.get("measured_exceeded")
            if exceeded is False:
                if not details.get("known_fault_quiet_since"):
                    self.analyzer.issue_manager.update_details(db_data, issue_id, {"known_fault_quiet_since": now.isoformat()})
            elif exceeded and details.get("known_fault_quiet_since"):
                self.analyzer.issue_manager.apply_action(db_data, [issue_id], "activate", reason="ismert hiba visszatért")
                self.analyzer.issue_manager.update_details(db_data, issue_id, {"known_fault_returned_at": now.isoformat()},
                                                           remove=["known_fault_quiet_since"])
                self.knowledge.store.setdefault("lessons", []).append({
                    "at": now.isoformat(), "issue_id": issue_id, "diagnostic": data.get("diagnostic_key", ""), "outcome": "known_fault_returned",
                    "label": "Ismert hiba visszatért", "note": "", "text": "Ismert hiba visszatért: az issue újranyílt"})
                reopened.append(issue_id)
        if reopened:
            self.knowledge.save()
        return reopened

    def _check_follow_ups(self, db_data: Dict[str, Any], now: datetime) -> List[Dict[str, Any]]:
        """Az utókövetések értékelése (a napi körben): javult / nem javult (újranyitás) / megismétlődött.

        Hívja: DiscoveryService._run."""
        tracked = self.knowledge.store.get("follow_ups", {})
        running = {iid: f for iid, f in tracked.items() if f.get("status") == "running"}
        if not running:
            return []
        issues = {iid: (data if isinstance(data, dict) else data.to_dict()) for iid, data in (db_data.get("issues") or {}).items()}
        changed = []
        for issue_id, follow in running.items():
            issue = issues.get(issue_id)
            if issue is None:   # törölt issue: nincs mit követni
                continue
            repeats: List[str] = []
            if follow["mode"] == "no_repeat":
                diagnostic = self.analyzer.diagnostic_registry.get(follow["diagnostic"])

                def entity_of(other: Mapping[str, Any]) -> List[Tuple[str, str]]:
                    _, items = self._threshold_context(db_data, other, follow["diagnostic"])
                    return items or (diagnostic.entity_items(other.get("subject", ""), other.get("details") or {}) if diagnostic else [])

                repeats = follow_ups.matching_repeats(follow, issue_id, follow["diagnostic"], issues.values(), entity_of)
            status, action = follow_ups.evaluate(follow, issue, now, repeats)
            follow.setdefault("checks", []).append({"at": now.isoformat(), "metric": issue.get("metric_value"),
                                                    "exceeded": (issue.get("details") or {}).get("measured_exceeded")})
            follow["checks"] = follow["checks"][-30:]
            follow["repeats"] = repeats
            if status == "running":
                continue
            follow["status"], follow["finished_at"] = status, now.isoformat()
            if action == "reopen":
                self.analyzer.issue_manager.apply_action(db_data, [issue_id], "activate", reason="utókövetés: nem javult")
            self.analyzer.issue_manager.update_details(db_data, issue_id, {
                "follow_up": {k: v for k, v in follow.items() if k not in ("issue_id", "diagnostic", "subject", "description")}})
            self.knowledge.store.setdefault("lessons", []).append({
                "at": now.isoformat(), "issue_id": issue_id, "diagnostic": follow["diagnostic"], "outcome": f"follow_up:{status}",
                "label": "Utókövetés", "note": "", "text": f"Utókövetés ({follow['label'].lower()}): {follow_ups.STATUS_LABELS[status]}"
                + (f" – új issue: {', '.join(repeats)}" if repeats else "")})
            changed.append({"issue_id": issue_id, "status": status})
        self.knowledge.save()
        return changed

    def follow_up_overview(self) -> List[Dict[str, Any]]:
        """Az utókövetések listája a Felülvizsgálathoz.

        Hívja: DiscoveryService.review."""
        items = list(self.knowledge.store.get("follow_ups", {}).values())
        order = lambda f: (f.get("status") not in follow_ups.ATTENTION, f.get("status") != "running", f.get("until", ""))
        return [{**f, "status_label": follow_ups.STATUS_LABELS.get(f.get("status"), f.get("status"))} for f in sorted(items, key=order)]

    def lesson_options(self, issue_id: str) -> Dict[str, Any]:
        """Egy issue lezárásának választható tanulságai és az űrlap alapértékei.

        Hívja: DashboardHandler.do_GET."""
        db_data = self.repository._load_db()
        with self._lock:
            issue, candidate, signal_items, diagnostic_key = self._issue_context(db_data, issue_id)
            threshold_defs, entity = self._threshold_context(db_data, issue, diagnostic_key)
            options = lesson_options(issue, candidate, signal_items, diagnostic_key, threshold_defs, entity)
            options["lesson"] = (issue.get("details") or {}).get("lesson")
            options["follow_up"] = (issue.get("details") or {}).get("follow_up")
            diagnostic = self.analyzer.diagnostic_registry.get(diagnostic_key)
            single = getattr(diagnostic, "family", "") == "SINGLE_TICKET"
            options["follow_up_modes"] = [{"key": k, "label": v} for k, v in follow_ups.MODES.items()
                                          if not (single and k == "improve")]
            options["state"] = issue.get("state")
            return options

    def close_issue_with_lesson(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Az issue lezárása, és a nyomozás tanulsága a tudásba (tény, szabály, ismert eltérés, megerősítés).

        Hívja: végpont POST /api/issues/lesson."""
        issue_id, outcome = str(payload.get("issue_id", "")), str(payload.get("outcome", ""))
        if outcome not in LESSON_OUTCOMES:
            raise ValueError(f"Ismeretlen tanulság: {outcome}")
        note = str(payload.get("note", "")).strip()
        groups = [str(g) for g in payload.get("groups") or [] if str(g).strip()]
        days = [str(d) for d in payload.get("days") or [] if str(d).strip()]
        feature = str(payload.get("feature", "")).strip()
        reanalyze_after = False
        with self.repository.transaction() as db_data:
            with self._lock:
                before = self._waiting_ids()
                issue, candidate, signal_items, diagnostic_key = self._issue_context(db_data, issue_id)
                remark = f"issue {issue_id} lezárása" + (f": {note}" if note else "")
                created: Dict[str, List[str]] = {"tény": [], "szabály": []}
                cid = candidate.candidate_id if candidate is not None else None
                if outcome == "confirmed":
                    if cid:
                        self.manager.decide(cid, Verdict.FINDING.value, "confirmed", note=remark)
                elif outcome in ("off_hours", "not_expected"):
                    if not groups:
                        raise ValueError("Adj meg legalább egy csoportot.")
                    if outcome == "off_hours" and not days:
                        raise ValueError("Jelöld be, mely napokon nem dolgozik a csoport.")
                    if outcome == "not_expected" and not feature:
                        raise ValueError("Hiányzik a funkció.")
                    pairs = [("nem_dolgozik", {"csoport": g, "nap": d}) for g in groups for d in days] if outcome == "off_hours" \
                        else [("nem_elvart", {"funkcio": feature, "csoport": g}) for g in groups]
                    for predicate, args in pairs:
                        created["tény"].append(self.manager.add_fact(predicate, args, note=remark).fact_id)
                    if cid and candidate.state != CandidateState.REJECTED.value:
                        self.manager.decide(cid, Verdict.NOT_FINDING.value, Scope.RULE.value, note=remark, fact=pairs[0])
                elif outcome == "false_alarm":
                    if cid:
                        decision = self.manager.decide(cid, Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value, note=remark)
                        created["szabály"].append(decision.rule_id)
                    else:
                        items = [tuple(i) for i in (payload.get("items") or [i for i in signal_items if i[0] not in ("incident", "subject")])]
                        if not items:
                            raise ValueError("Jelöld be, mire szóljon a szabály (legalább egy elem).")
                        rule = self.manager.add_rule([{"dim": d, "values": [v]} for d, v in items], measures=[diagnostic_key],
                                                     reason=remark, source=f"lesson:{issue_id}")
                        created["szabály"].append(rule.rule_id)
                elif outcome == "adjust_threshold":
                    scope = [list(i) for i in payload.get("scope") or []]
                    params = {str(k): float(v) for k, v in (payload.get("params") or {}).items() if str(v).strip() != ""}
                    defs, _ = self._threshold_context(db_data, issue, diagnostic_key)
                    current = {d["key"]: d["value"] for d in defs}
                    params = {k: (int(v) if float(v).is_integer() else v) for k, v in params.items() if k in current and v != current[k]}
                    if not scope:
                        raise ValueError("Jelöld be, mire szóljon a küszöb (legalább egy csoport, útvonal vagy service).")
                    if not params:
                        raise ValueError("Adj meg legalább egy, a mostanitól eltérő küszöböt.")
                    override = self.knowledge.add_threshold_override(diagnostic_key, scope, params, "kézi", remark, self.manager.now)
                    created["küszöb"] = [override.override_id]
                    reanalyze_after = True
                elif outcome == "known_deviation":
                    if not cid or candidate.kind != "contrast":
                        raise ValueError("Ismert eltérés csak feltárási mintából lett issue-nál rögzíthető.")
                    configure(self.manager, self.config)
                    backfill_counts(self.manager, self._incidents(db_data), candidate, self._now(), self.config)
                    self.manager.decide(cid, Verdict.KNOWN_DEVIATION.value, note=remark)
                self.analyzer.issue_manager.apply_action(db_data, [issue_id], "resolve", reason=f"lesson:{outcome}")
                lesson = {"at": self.manager.now, "issue_id": issue_id, "diagnostic": diagnostic_key, "outcome": outcome,
                          "label": LESSON_OUTCOMES[outcome]["label"], "note": note, "groups": groups, "days": days,
                          "feature": feature, "candidate_id": cid, "created": created,
                          "text": describe_lesson(outcome, created, groups, days, feature)}
                details: Dict[str, Any] = {}   # az issue-ra kerülő tanuló-adatok; a végén az IssueManager írja
                plan = payload.get("follow_up") or {}
                if plan.get("mode"):
                    follow = follow_ups.start(str(plan["mode"]), int(plan.get("days", 14)), self.manager.now or self._now().isoformat(),
                                              issue, [list(i) for i in plan.get("scope") or []])
                    details["follow_up"] = follow
                    self.knowledge.store.setdefault("follow_ups", {})[issue_id] = {
                        **follow, "issue_id": issue_id, "diagnostic": diagnostic_key, "subject": issue.get("subject", ""),
                        "description": issue.get("description", "")}
                    lesson["text"] += f" · utókövetés: {follow['label'].lower()}, {follow['days']} nap"
                effect_lines = {
                    "confirmed": ["A tipp erős címkét kapott: a hasonló jelöltekre nagyobb eséllyel mond „feladat”-ot.",
                                  "Ismert hiba: ha a mérés elcsendesedik, majd újra túllép, az issue magától újranyílik."],
                    "off_hours": ["Tény: a csoport ezeken a napokon nem dolgozik – az időfüggő jelöltek ezekre a napokra nem jönnek."],
                    "not_expected": ["Tény: a funkció ennél a csoportnál nem elvárt – a funkcióhasználati jelzés nem jön."],
                    "false_alarm": ["Szabály: az ilyen minta (jelzés) többé nem jön."],
                    "adjust_threshold": ["Szűkebb küszöb: ennél a hatókörnél ez számít a globális helyett; minden újraértékelődik."],
                    "known_deviation": ["Ismert eltérés: a minta a saját normájához mérve jön csak vissza (ha romlik)."],
                    "none": ["A tudás nem változott."],
                }.get(outcome, [])
                if outcome == "false_alarm" and candidate is None and diagnostic_key:
                    effect_lines = [f"A(z) {self._key_label(diagnostic_key)} detektor az ilyen esetre többé nem nyit issue-t – a valós "
                                    f"idejű mérésben sem, és jóváhagyásos módban jelzés sem jön. A már nyitott issue-k mérése folytatódik."]
                effect_lines += [f"Tény: {fid}" for fid in created.get("tény", [])]
                effect_lines += [f"Szűkebb küszöb: {oid}" for oid in created.get("küszöb", [])]
                if plan.get("mode"):
                    effect_lines.append(f"Utókövetés: {details['follow_up']['label'].lower()}, {details['follow_up']['days']} napig.")
                effect = self._effect(before, f"Lezárva: {LESSON_OUTCOMES[outcome]['label']}", effect_lines,
                                      exclude=[cid] if cid else [], rule_ids=created.get("szabály", []))
                lesson["effect"] = {k: effect[k] for k in ("lines", "rules", "hidden", "returned")}
                details["lesson"] = lesson
                self.analyzer.issue_manager.update_details(db_data, issue_id, details)
                self.knowledge.store.setdefault("lessons", []).append(lesson)
                self.knowledge.save()
                self._save()
            if reanalyze_after:   # az új küszöb a többi (nyitott) mérésre is azonnal hat
                self.analyzer.reanalyze_all(db_data)
        return {"lesson": lesson, "message": "Lezárva. Tanulság: " + lesson["text"], "effect": effect}

    # a gyári detektorok, amelyeknél a gép küszöböt tanul (jegyszintű, mérsékelt / extrém küszöbös mérés)
    LEARNED_THRESHOLD_DETECTORS = ("PING_PONG", "FLAPPING_INCIDENT")

    def _threshold_suggestions(self, now: datetime) -> List[Dict[str, Any]]:
        """Tanult küszöb-javaslatok: ahol egy entitás (service, csoport, alkategória) jegyeinek többsége túllépi a
        detektor globális küszöbét, ott a túllépés a szokásos (models.discovery.learned_thresholds).

        Hívja: DiscoveryService.review."""
        db_data = self.repository._load_db()
        manager = self.analyzer.threshold_manager
        dismissed = self.knowledge.store.get("threshold_suggestion_dismissed", {})
        result = []
        for key in self.LEARNED_THRESHOLD_DETECTORS:
            diagnostic = self.analyzer.diagnostic_registry.get(key)
            if diagnostic is None or diagnostic.config_key in manager.disabled:
                continue
            try:
                diagnostic, ctx = self.analyzer.context_for(db_data, diagnostic.config_key, Trigger.MANUAL, now)
                opened = {str(i.id): t.opened_at for i in ctx.incidents if (t := incident_timeline(i, now)) is not None}
                rows = [(diagnostic.signal_items(o.subject, o.details), o.details.get(diagnostic.metric_key, 0), opened.get(o.subject))
                        for o in diagnostic._observe(ctx, diagnostic.resolve_window(ctx))]
                existing = [tuple(tuple(i) for i in o.scope) for o in self.knowledge.threshold_overrides()
                            if o.state == "active" and o.diagnostic == key]
                result += learned_threshold_suggestions(key, self._key_label(key), diagnostic.thresholds(ctx), rows, existing, dismissed)
            except Exception as error:   # egy detektor hibája nem akaszthatja meg a felülvizsgálatot
                print(f"--> [FELTÁRÁS] {key} küszöb-javaslatai nem készültek el: {error}", flush=True)
        return result

    def threshold_override_action(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Szűkebb küszöb: tanult javaslat elfogadása, elutasítása, vagy egy küszöb visszavonása (utána minden
        újraértékelődik).

        Hívja: felület (POST /api/discovery/threshold-override)."""
        op = payload.get("op")
        if op == "dismiss_suggestion":
            store = self.knowledge.store.setdefault("threshold_suggestion_dismissed", {})
            store[str(payload.get("key", ""))] = str(payload.get("value", ""))
            self.knowledge.save()
            return {"message": "Rendben, ezt a küszöb-javaslatot nem hozza újra (csak ha a szokásos érték változik)."}
        if op == "accept_suggestion":
            diagnostic, scope = str(payload.get("diagnostic", "")), [list(i) for i in payload.get("scope") or []]
            params = {str(k): (int(float(v)) if float(v).is_integer() else float(v)) for k, v in (payload.get("params") or {}).items()}
            if not diagnostic or not scope or not params:
                raise ValueError("Hiányzik a detektor, a hatókör vagy a küszöb.")
            label = self._key_label(diagnostic)
            override = self.knowledge.add_threshold_override(diagnostic, scope, params, "tanult", str(payload.get("note", "")) or
                                                             "tanult: ott a túllépés a szokásos", self.manager.now)
            value = self.analyzer.issue_manager._issue_value
            active = lambda db: sum(1 for i in db.get("issues", {}).values()
                                    if value(i, "diagnostic_key") == diagnostic and str(value(i, "state")) == "active")
            with self.repository.transaction() as db_data:
                before = active(db_data)
                self.analyzer.reanalyze_all(db_data)
                after = active(db_data)
            scope_text = " & ".join(f"{d} = {v}" for d, v in scope)
            values = ", ".join(f"{k} = {v}" for k, v in params.items())
            effect = {"title": "Tanult küszöb", "rules": [], "warning": "", "hidden": 0, "returned": 0, "lines": [
                f"{label}: ennél a hatókörnél ({scope_text}) mostantól {values} számít a globális helyett ({override.override_id}).",
                f"Minden újraértékelődött: az aktív {label} issue-k száma {before} → {after}.",
                "A kézzel adott szűkebb küszöb továbbra is nyer felette; a Thresholdok nézet „Egyéni küszöbök” alatt törölhető."]}
            return {"override": override.to_dict(), "message": f"Tanult küszöb: {scope_text}: {values}", "effect": effect}
        if op != "revoke":
            raise ValueError("A művelet: revoke | accept_suggestion | dismiss_suggestion")
        self.knowledge.revoke_threshold_override(str(payload.get("override_id", "")))
        with self.repository.transaction() as db_data:
            self.analyzer.reanalyze_all(db_data)
        return {"message": "A szűkebb küszöb visszavonva; ott újra a globális küszöb számít."}

    def unrefine(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """A bontás visszavonása: a szülő újra önálló jelölt.

        Hívja: felület (POST /api/discovery/unrefine)."""
        candidate = self._mutate(lambda: self.manager.unrefine(str(payload.get("candidate_id", ""))))
        return {"candidate_id": candidate.candidate_id, "state": candidate.state}

    def review(self) -> Dict[str, Any]:
        """A Felülvizsgálat adatai: erős jelölt elnyomás alatt, felülvizsgálandó szabályok, tények, almérések,
        általánosítás, munkaidő, tanult küszöbök, utókövetések.

        Hívja: DashboardHandler.do_GET, felület (GET /api/discovery/review)."""
        incidents = self._incidents()
        with self._lock:
            now = self._now(incidents)
            params = self.config.review_params()
            view = self.knowledge.view(now.isoformat())
            timelines = [t for i in incidents if (t := incident_timeline(i, now)) is not None]
            usage = self.analyzer.diagnostic_registry.get("FEATURE_USAGE")
            catalog = usage.catalog if usage is not None else DEFAULT_CATALOG
            items = review_rules(self.manager.rules(), now, params) + review_facts(
                self.knowledge.facts(), timelines, now, params, catalog, view.levels)
            rendered = {r.rule_id: r.render() for r in self.manager.rules()}
            rendered.update({f.fact_id: f.render() for f in self.knowledge.facts()})
            child_params = self.config.child_review_params()
            return {"now": now.isoformat(),
                    "items": self._strong_under_rules(rendered)
                    + [{**item.__dict__, "rendered": rendered.get(item.object_id, "")} for item in items]
                    + review_children(self.manager.children(), now, child_params),
                    "generalizations": self._generalizations(),
                    "schedules": self._schedule_suggestions(timelines, now, view),
                    "thresholds": self._threshold_suggestions(now),
                    "follow_ups": self.follow_up_overview()}

    def _strong_under_rules(self, rendered: Mapping[str, str]) -> List[Dict[str, Any]]:
        """Biztonsági szelep: egy elvető szabály most egy nagyon erős (áttörő erősségű) jelöltet rejt, amely a szabály
        létrejötte után is megjelent. Lehet, hogy az elnyomott helyen valódi hiba jött – a Felülvizsgálat elejére.

        Hívja: DiscoveryService.review."""
        rules = {r.rule_id: r for r in self.manager.rules() if r.state == "active"}
        found: Dict[str, List[Candidate]] = {}
        for candidate in self.manager.candidates():
            rule_id = (candidate.filtered_by or {}).get("rule_id")
            rule = rules.get(rule_id)
            if rule is None or candidate.kind == "signal" or self.manager.strength(candidate) < self.manager.extreme_strength:
                continue
            if candidate.last_seen_at and rule.created_at and candidate.last_seen_at < rule.created_at:
                continue
            found.setdefault(rule_id, []).append(candidate)
        items = []
        for rule_id, hidden in found.items():
            hidden.sort(key=lambda c: -self.manager.strength(c))
            top = hidden[0]
            s = top.stats
            items.append({"kind": "rule", "object_id": rule_id, "reason": "strong_under_rule", "rendered": rendered.get(rule_id, ""),
                          "evidence": len(hidden), "candidate_id": top.candidate_id,
                          "message": (f"Ez a szabály most {len(hidden)} nagyon erős jelöltet rejt el – a legerősebb: {self._label(top)}: {top.key} "
                                      f"({s.get('bad_count')}/{s.get('group_size')} érintett, kiemelés {float(s.get('lift') or 0):.1f}×, "
                                      f"q={float(s.get('q_value') or 1):.1e}). Lehet, hogy az elnyomott helyen valódi hiba jött: nézd meg, "
                                      f"és ha kell, vond vissza a szabályt.")})
        return items

    def learning_balance(self, weeks: int = 8) -> List[Dict[str, Any]]:
        """Hetente: hány jelölt jött, mennyi döntés, ebből feladat / most nem / bontás / elnyomás; a lezárt feladatokból
        valódi hiba és nem hiba; a visszatérő zaj és a tipp egyezése. Ebből látszik, hogy a tanulás javít-e.

        Hívja: DiscoveryService.overview."""
        def week_of(value: str) -> str:
            try:
                day = datetime.fromisoformat(str(value)[:19])
            except ValueError:
                return ""
            return (day - timedelta(days=day.weekday())).date().isoformat()

        rows: Dict[str, Dict[str, Any]] = {}
        row = lambda key: rows.setdefault(key, {"week": key, "new": 0, "decisions": 0, "finding": 0, "not_now": 0, "returning": 0,
                                                "refine": 0, "suppressed": 0, "confirmed": 0, "false_alarm": 0, "other_lessons": 0,
                                                "tips": 0, "tips_agreed": 0, "auto": 0})
        for c in self.manager.candidates():
            if c.first_seen_at:
                row(week_of(c.first_seen_at))["new"] += 1
        seen_not_now: Dict[str, int] = {}
        for d in self.manager.decisions():
            key = week_of(d.at)
            if not key:
                continue
            r = row(key)
            if d.auto:
                r["auto"] += 1
                continue
            if d.scope in ("confirmed", "investigated"):
                continue   # a lezárás tanulsága lent számít
            r["decisions"] += 1
            if d.verdict == Verdict.FINDING.value:
                r["finding"] += 1
            elif d.verdict == Verdict.REFINE.value:
                r["refine"] += 1
            elif d.scope == Scope.THIS_OCCURRENCE.value:
                r["not_now"] += 1
                seen_not_now[d.candidate_id] = seen_not_now.get(d.candidate_id, 0) + 1
                if seen_not_now[d.candidate_id] >= 2:
                    r["returning"] += 1
            elif d.scope in (Scope.THIS_PATTERN.value, Scope.RULE.value):
                r["suppressed"] += 1
            if d.suggestion_snapshot:
                r["tips"] += 1
                r["tips_agreed"] += int((d.suggestion_snapshot.get("verdict") == Verdict.FINDING.value) == (d.verdict == Verdict.FINDING.value))
        for rule in self.manager.rules():
            if rule.source.startswith(("element:", "dimension:")) and rule.created_at:
                row(week_of(rule.created_at))["suppressed"] += 1
        for lesson in self.knowledge.store.get("lessons", []):
            key = week_of(lesson.get("at", ""))
            if not key:
                continue
            outcome = lesson.get("outcome")
            row(key)["confirmed" if outcome == "confirmed" else "false_alarm" if outcome == "false_alarm" else "other_lessons"] += 1
        result = sorted((r for r in rows.values() if r["week"]), key=lambda r: r["week"])[-weeks:]
        for r in result:
            closed = r["confirmed"] + r["false_alarm"]
            r["confirmed_share"] = r["confirmed"] / closed if closed else None
            r["tip_agreement"] = r["tips_agreed"] / r["tips"] if r["tips"] else None
        return result

    def config_schema(self) -> Dict[str, Any]:
        """A Paraméterek fül: szakaszonként a paraméterek definíciója és értéke.

        Hívja: DashboardHandler.do_GET, DiscoveryService.update_config, felület (GET /api/discovery/config)."""
        return {section: {d.key: {**d.to_dict(), "value": self.config.get(section, d.key)} for d in definitions}
                for section, definitions in DEFINITIONS.items()}

    # ------------------------------------------------------------------
    # Döntések és tudás
    # ------------------------------------------------------------------

    def decide(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """A jelölt három művelete: Feladat (issue nyílik), Most nem, Bontsd tovább (almérések, próbálkozások,
        bontás); a válaszban a hatás.

        Hívja: felület (POST /api/discovery/decide)."""
        action, candidate_id = payload.get("action"), str(payload.get("candidate_id", ""))
        note = str(payload.get("note", ""))
        if action not in DECISIONS:
            raise ValueError(f"Ismeretlen döntés: {action} (lehetséges: {', '.join(DECISIONS)})")
        with self._lock:
            if candidate_id not in self.manager.state["candidates"]:
                raise ValueError(f"Nincs ilyen jelölt: {candidate_id}. Lehet, hogy egy reset óta már nem létezik; "
                                 f"frissítsd a jelöltsort.")
        with self.repository.transaction() as db_data:
            with self._lock:
                before = self._waiting_ids()
                candidate = self.manager.candidate(candidate_id)
                result: Dict[str, Any] = {"candidate_id": candidate_id, "action": action}
                if action == "finding" and candidate.kind == "signal":
                    self.manager.decide(candidate_id, Verdict.FINDING.value, note=note)
                    issue_id = self._accept_signal(db_data, candidate)
                    self.manager.link_issue(candidate_id, issue_id)
                    result["issue_id"] = issue_id
                elif action == "finding":
                    self.manager.decide(candidate_id, Verdict.FINDING.value, note=note)
                    now = self._now()
                    diagnostic, ctx = self.analyzer.context_for(db_data, "pattern_finding", Trigger.MANUAL, now)
                    issue = promote_finding(db_data, self.analyzer.issue_manager, diagnostic, candidate, ctx,
                                            self.config.get(MEASURES_SECTION, candidate.measure))
                    self.manager.link_issue(candidate_id, issue.issue_id if issue else None)
                    result["issue_id"] = issue.issue_id if issue else None
                elif action == "reject_once":
                    self.manager.decide(candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value, note=note)
                elif action == "refine":
                    self.manager.decide(candidate_id, Verdict.REFINE.value, note=note)
                    try:
                        self._run_children(db_data, self._now(), [self.manager.candidate(candidate_id)])
                    except Exception as error:
                        print(f"--> [FELTÁRÁS] {candidate_id} almérései nem futottak le: {error}", flush=True)
                    result["probes"] = self._run_probes(db_data, self._now(), [self.manager.candidate(candidate_id)])
                    if candidate.measure in MEASURES_BY_KEY:   # jelzésnél nincs mezőbontás
                        try:
                            refined = self.manager.candidate(candidate_id)
                            as_of = datetime.fromisoformat(refined.window_end) if refined.window_end else self._now()
                            self._refresh_breakdown(refined, self._profiles(self._incidents(db_data), as_of), as_of)
                        except Exception as error:
                            print(f"--> [FELTÁRÁS] {candidate_id} bontása nem készült el: {error}", flush=True)
                    result["message"] = ("Tovább bontva: a Csoportos minták közt vizsgálhatod. " +
                                         (f"A gép {result['probes']} próbálkozása eltér az érintetteknél – jelöld, melyik jó módszer. "
                                          if result["probes"] else "A gép próbálkozásai közül egyik sem tér el érdemben. ") +
                                         "Ami ezen belül kiugrik, vagy amit egy riasztó almérés talál, újra eléd kerül.")
                self._save()
                result["effect"] = self._decide_effect(action, candidate_id, before, result)
        return result

    def fact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Tény felvétele vagy visszavonása (utána minden újraértékelődik).

        Hívja: felület (POST /api/discovery/fact)."""
        op = payload.get("op")
        if op == "add":
            args = {str(k): str(v).strip() for k, v in (payload.get("args") or {}).items()}
            fact = self._mutate(lambda: self.manager.add_fact(str(payload["predicate"]), args, str(payload.get("valid_until", "")),
                                                              str(payload.get("note", ""))), reanalyze=True)
        elif op == "revoke":
            fact = self._mutate(lambda: self.manager.revoke_fact(str(payload["fact_id"])), reanalyze=True)
        elif op == "confirm":
            fact = self._mutate(lambda: self.knowledge.confirm_fact(str(payload["fact_id"]), self._now().isoformat()))
        else:
            raise ValueError("A művelet: add | revoke | confirm")
        return {"fact": {**fact.to_dict(), "rendered": fact.render()}}

    def rule(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Szabály visszavonása vagy megerősítése (a mező kikapcsolásának visszavonása a szerepét is visszaállítja).

        Hívja: felület (POST /api/discovery/rule)."""
        op, rule_id = payload.get("op"), str(payload.get("rule_id", ""))
        with self._lock:
            before = self._waiting_ids()
        if op == "revoke":
            rule = self._mutate(lambda: self.manager.revoke_rule(rule_id, str(payload.get("reason", ""))))
            if rule.source.startswith("dimension:"):
                dim = rule.source.split(":", 1)[1]
                if dim in DIMENSION_ROLE_DEFAULTS:
                    self._mutate(lambda: self.config.update(DIMENSIONS_SECTION, dim, DIMENSION_ROLE_DEFAULTS[dim]))
        elif op == "confirm":
            rule = self._mutate(lambda: self.manager.confirm_rule(rule_id))
        else:
            raise ValueError("A művelet: revoke | confirm")
        with self._lock:
            effect = self._effect(before, "Szabály visszavonva" if op == "revoke" else "Szabály megerősítve",
                                  ["Az általa elrejtett jelöltek újra döntésre várhatnak (ha még fennállnak)." if op == "revoke"
                                   else "A szabály érvényben marad; a felülvizsgálat ettől számol."])
        return {"rule": {**rule.to_dict(), "rendered": rule.render()}, "effect": effect}

    def availability(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Válasz a funkcióhasználati kérdésre: elérhető-e a funkció a csoportnak (csoport "*" = mindenkinek).

        Hívja: felület (POST /api/discovery/availability)."""
        predicate = "elerheto" if payload.get("available") else "nem_elerheto"
        args = {"funkcio": str(payload["feature"]), "csoport": str(payload["group"])}
        fact = self._mutate(lambda: self.manager.add_fact(predicate, args), reanalyze=True)
        # a jelzések azonnal frissülnek: a megválaszolt kérdés eltűnik, vagy edukációs jelzés lesz belőle
        with self.repository.transaction() as db_data:
            with self._lock:
                self._ingest_signals(db_data, self._now())
                self._save()
        return {"fact": {**fact.to_dict(), "rendered": fact.render()}}

    def update_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Egy feltárási paraméter módosítása.

        Hívja: felület (POST /api/discovery/config)."""
        with self._lock:
            value = self.config.update(str(payload["section"]), str(payload["key"]), payload["value"])
            configure(self.manager, self.config)
        return {"section": payload["section"], "key": payload["key"], "value": value, "config": self.config_schema()}
