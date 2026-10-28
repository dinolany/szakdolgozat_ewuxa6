"""A feltárási ciklus kezelője: jelöltek fogadása, szabályok, tipp, sorrend, döntések, almérések és bontás.

Az állapot egy JSON-kompatibilis szótár ({candidates, decisions, rules, meta}), így a
kezelő bármilyen tárolóra ráültethető. Az idő mindig a mérés (virtuális) ideje, nem a valós óra:
így a napról napra visszajátszás determinisztikus.
"""

from datetime import datetime, timedelta
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from learning.children.child_def import ChildDef, ROLES as CHILD_ROLES
from learning.children.child_runner import with_previous
from learning.children.expression_language import compile_expression
from learning.ingest_report import IngestReport
from learning.mining.baseline import compare_to_baseline, freeze_baseline, non_overlapping
from learning.mining.clusters import find_clusters, reach
from learning.mining.contrast_mining import PatternResult, pattern_key, with_implied
from learning.state.candidate import Candidate, CandidateState, make_candidate_id
from learning.state.decision import Decision, Scope, Verdict
from learning.state.rule import Rule, RuleAction, RuleKind, match_rule
from model.knowledge.fact import Fact
from model.knowledge.knowledge_view import KnowledgeView
from model.knowledge.predicates import NEM_DOLGOZIK, PREDICATES
from model.knowledge.rule_state import RuleState
from storage.knowledge_base import KnowledgeBase


# Ezekben az állapotokban dönt a szabályrendszer a láthatóságról
_OPEN_STATES = {CandidateState.QUEUED.value, CandidateState.FILTERED.value}

# Ezekből a döntésekből tanul a tipp
_LEARNING_VERDICTS = {Verdict.FINDING.value, Verdict.NOT_FINDING.value}

# Az egyszeri elvetés gyenge jel ("most zaj"), nem azt mondja, hogy a mintafajta érdektelen
# A döntés súlya a tippben: az egyszeri elvetés gyenge; a lezárt issue nyomozással igazolt tanulsága erős
# Az issue megnyitása („Feladat”) még csak azt mondja, hogy érdemes utánanézni: gyengébb jelzés; a lezárás
# nyomozással igazolt tanulsága („confirmed”) az erős
# A tipp erős címkéje az issue lezárásakor adott tanulság. A feladat nyitása (None) nem címke; a "most nem" gyenge
# ellenpélda (0.3): nélküle a tipp csak a lezárt hibákból tanulna, és a hibás csoport zajára is "feladat"-ot mondana
# (a tanulási szimulációban 42% helyett 72% egyezés). A visszatérő "most nem" előrébb sorolását a not_now számláló
# külön intézi, ezt a gyenge címke nem írja felül.
_SCOPE_WEIGHTS = {Scope.THIS_OCCURRENCE.value: 0.3, None: 0.0, "confirmed": 1.5, "investigated": 1.5}

class DiscoveryManager:
    def __init__(self, state: Optional[Dict[str, Any]] = None, extreme_strength: float = 6.0, signal_return_days: int = 7,
                 knowledge: Optional[KnowledgeBase] = None):
        """A feltárási állapot (jelöltek, döntések, szabályok, meta) és a közös tudástár; a régi, megszűnt figyelés
        alatti jelölteket újra döntésre teszi.

        Létrehozza: discovery_cli, synthetic.learning_sim, learning.discovery_service."""
        self.state = state if state is not None else {}
        for key, default in (("candidates", {}), ("decisions", []), ("rules", {}), ("meta", {})):
            self.state.setdefault(key, default)
        # a megszűnt "figyelés" alatti jelöltek (régi állapot) újra döntésre várnak
        for data in self.state["candidates"].values():
            if data.get("state") == "watching":
                data["state"] = CandidateState.QUEUED.value
                data["flags"] = sorted(set(data.get("flags") or []) | {"returned"})
        # A tények a közös tudástárban élnek; nélküle (pl. tesztben) a feltárási állapot része
        self.knowledge = knowledge if knowledge is not None else KnowledgeBase(store=self.state)
        self.extreme_strength = extreme_strength
        # a "most nem"-mel félretett jelzés ennyi nap után jöhet vissza (a minták az ablakuk után)
        self.signal_return_days = signal_return_days
        # Ismert eltérés (a konfiguráció felülírja)
        self.baseline_min_size = 30
        self.baseline_prior = 10.0
        self.baseline_max_p = 0.05
        self.baseline_min_ratio = 1.5
        self.baseline_min_bad = 3
        # Döntési kapacitás (a konfiguráció felülírja): betanításkor nagyobb, utána tartós
        self.decision_capacity = 10
        self.training_capacity = 25
        self.training_decisions = 150
        # Kiemelt dimenziók (a konfiguráció felülírja): a rájuk vonatkozó jelöltek előrébb kerülnek
        self.emphasized_dimensions: frozenset = frozenset()

    # ------------------------------------------------------------------
    # Hozzáférés
    # ------------------------------------------------------------------

    not_now_boost = 3.0   # prioritás-többlet minden korábbi "most nem" után

    @property
    def now(self) -> str:
        """A mérés (virtuális) ideje: a legutóbbi feltárásé.

        Hívja: DiscoveryManager._ensure_knowledge_rule (átadva), DiscoveryManager.add_child (átadva),
        DiscoveryManager.add_fact (átadva), DiscoveryManager.add_rule (átadva), DiscoveryManager.apply_rules (átadva),
        DiscoveryManager.block_breakdown_path (átadva), DiscoveryManager.breakdown_feedback (átadva),
        DiscoveryManager.capacity (átadva), DiscoveryManager.child_method_feedback (átadva) és mások."""
        return self.state["meta"].get("now", "")

    def candidate(self, candidate_id: str) -> Candidate:
        """Egy jelölt az azonosítója szerint.

        Hívja: DiscoveryCli.cmd_known, DiscoveryCli.cmd_log, DiscoveryCli.select, DiscoveryManager._labelled,
        DiscoveryManager.breakdown_feedback, DiscoveryManager.child_method_feedback, DiscoveryManager.decide,
        DiscoveryManager.ingest, DiscoveryManager.link_issue és mások."""
        return Candidate.from_dict(self.state["candidates"][candidate_id])

    def candidates(self) -> List[Candidate]:
        """Az összes jelölt.

        Hívja: DiscoveryCli._report_hidden, DiscoveryCli.cmd_hidden, DiscoveryCli.cmd_stats, DiscoveryCli.select,
        DiscoveryManager._waiting, DiscoveryManager.active_candidates, DiscoveryManager.apply_rules,
        DiscoveryManager.attach_to_open_issues, DiscoveryManager.contexts és mások."""
        return [Candidate.from_dict(data) for data in self.state["candidates"].values()]

    def facts(self) -> List[Fact]:
        """A tudástár tényei.

        Hívja: DiscoveryCli.cmd_facts, DiscoveryManager.apply_rules."""
        return self.knowledge.facts()

    def rules(self) -> List[Rule]:
        """Az összes szabály (aktív és visszavont).

        Hívja: DiscoveryCli.cmd_review, DiscoveryCli.cmd_rules, DiscoveryManager._ensure_knowledge_rule,
        DiscoveryManager.apply_rules, DiscoveryManager.reopen_rejected_by_revoked_rules,
        DiscoveryService._generalizations, DiscoveryService._strong_under_rules, DiscoveryService.knowledge_overview,
        DiscoveryService.learning_balance és mások."""
        return [Rule.from_dict(data) for data in self.state["rules"].values()]

    def decisions(self) -> List[Decision]:
        """Az összes döntés, időrendben.

        Hívja: DiscoveryCli.cmd_log, DiscoveryManager._labelled, DiscoveryManager.capacity,
        DiscoveryManager.learning_stats, DiscoveryManager.reopen_rejected_by_revoked_rules,
        DiscoveryService._refinements, DiscoveryService.knowledge_overview, DiscoveryService.learning_balance,
        DiscoveryService.overview."""
        return [Decision.from_dict(data) for data in self.state["decisions"]]

    def _save_candidate(self, candidate: Candidate) -> None:
        """A jelölt visszaírása az állapotba.

        Hívja: DiscoveryManager.apply_rules, DiscoveryManager.attach_to_open_issues,
        DiscoveryManager.breakdown_feedback, DiscoveryManager.child_method_feedback, DiscoveryManager.decide,
        DiscoveryManager.ingest, DiscoveryManager.ingest_signals, DiscoveryManager.link_issue,
        DiscoveryManager.probe_feedback és mások."""
        self.state["candidates"][candidate.candidate_id] = candidate.to_dict()

    def _save_rule(self, rule: Rule) -> None:
        """A szabály visszaírása az állapotba.

        Hívja: DiscoveryManager._ensure_knowledge_rule, DiscoveryManager.add_rule, DiscoveryManager.apply_rules,
        DiscoveryManager.confirm_rule, DiscoveryManager.revoke_rule, DiscoveryService._suppress, learning_sim._decide."""
        self.state["rules"][rule.rule_id] = rule.to_dict()

    def _next_id(self, prefix: str) -> str:
        """Új, sorszámozott azonosító (RULE-0001, DEC-0001…); a számláló a resetet is túléli.

        Hívja: DiscoveryManager._ensure_knowledge_rule, DiscoveryManager.add_rule, DiscoveryManager.decide."""
        counter = self.state["meta"].setdefault("counters", {})
        counter[prefix] = counter.get(prefix, 0) + 1
        return f"{prefix}-{counter[prefix]:04d}"

    # ------------------------------------------------------------------
    # Feltárási eredmények fogadása
    # ------------------------------------------------------------------

    def ingest(
        self,
        results: Iterable[PatternResult],
        measured_at: datetime,
        window_start: datetime,
        window_end: datetime,
        counts: Optional[Mapping[str, Mapping[str, int]]] = None,
    ) -> IngestReport:
        """A feltárás eredményeinek fogadása.

        `counts`: jelöltazonosítónként a minta darabszámai ebben az ablakban (az ismert jelöltekre
        akkor is, ha most nem voltak kiugróak); ebből lesz a szokásos eltérés, és ehhez méri
        a rendszer az ismert eltérésként kezelt jelölteket.
        

        Hívja: runner.run_discovery."""
        now = measured_at.isoformat()
        self.state["meta"]["now"] = now
        self.state["meta"]["last_run"] = {"measured_at": now, "window_start": window_start.isoformat(),
                                          "window_end": window_end.isoformat()}
        report = IngestReport(now)
        seen = set()

        for result in results:
            candidate_id = make_candidate_id(result.measure, result.candidate_key)
            seen.add(candidate_id)
            stats = {
                "bad_count": result.bad_count, "group_size": result.group_size, "population": result.population,
                "rate": result.rate, "base_rate": result.base_rate, "lift": result.lift, "excess": result.excess,
                "p_value": result.p_value, "q_value": result.q_value, "distinct_days": result.distinct_days,
                **(result.extra or {}),
            }
            data = self.state["candidates"].get(candidate_id)
            if data is None:
                candidate = Candidate(candidate_id, result.measure, result.key, first_seen_at=now, kind=result.kind)
                report.new.append(candidate_id)
            else:
                candidate = Candidate.from_dict(data)
                report.updated.append(candidate_id)
            new_ids = set(result.bad_incident_ids) - set(candidate.bad_incident_ids)
            candidate.alternatives = list(result.alternative_keys)
            candidate.stats = stats
            candidate.bad_incident_ids = list(result.bad_incident_ids)
            candidate.window_start, candidate.window_end = window_start.isoformat(), window_end.isoformat()
            candidate.last_seen_at = now
            candidate.occurrences.append({"measured_at": now, "bad_count": result.bad_count,
                                          "lift": round(result.lift, 3), "new_bad": len(new_ids)})

            # Egyszer elvetett jelölt: ha az elvetett ablakon túl, új rossz ticketekkel bukkan fel, visszajön
            if candidate.state == CandidateState.REJECTED_ONCE.value and window_start.isoformat() >= candidate.rejected_window_end:
                candidate.state = CandidateState.QUEUED.value
                candidate.flags = sorted(set(candidate.flags) | {"returned"})
                report.returned.append(candidate_id)
            self._save_candidate(candidate)

        # Darabszámok és az ismert eltérések mérése a saját szokásos eltérésükhöz
        for candidate_id, values in (counts or {}).items():
            if candidate_id not in self.state["candidates"]:
                continue
            record = {"measured_at": now, "window_start": window_start.isoformat(),
                      "window_end": window_end.isoformat(), **values}
            candidate = self._append_counts(self.candidate(candidate_id), record)
            if candidate.state == CandidateState.BASELINED.value and not candidate.baseline:
                # az elemző már döntött; most gyűlt-e össze elég adat a normához?
                candidate.baseline = freeze_baseline(candidate.counts, now, self.baseline_min_size, self.baseline_prior)
                if candidate.baseline:
                    candidate.flags = [flag for flag in candidate.flags if flag != "baseline_pending"]
            elif candidate.state == CandidateState.BASELINED.value and candidate.baseline:
                if self._exceeds_baseline(candidate, record):
                    candidate.state = CandidateState.QUEUED.value
                    candidate.flags = sorted(set(candidate.flags) | {"exceeds_baseline"})
                    report.exceeds_baseline.append(candidate_id)
            self._save_candidate(candidate)

        self.state["meta"]["last_seen"] = sorted(seen)
        report.filtered = self.apply_rules()
        self.refresh_suggestions()
        return report

    def ingest_signals(self, signals: Iterable[Mapping[str, Any]], measured_at: datetime) -> List[str]:
        """Jóváhagyásos diagnosztikák jelzései jelöltként (kind="signal").

        Egy jelzés: {diagnostic, subject, items, details, description, severity, affected}. A jelölt
        ugyanúgy döntésre vár, mint a feltárás jelöltjei (tipp, csomópont, keret, szabály); elfogadásból
        lesz feladat (issue). A most nem jelző, korábbi jelzés-jelöltek kiesnek a sorból.
        

        Hívja: DiscoveryService._ingest_signals, learning_sim._day."""
        now = measured_at.isoformat()
        if now > self.state["meta"].get("now", ""):
            self.state["meta"]["now"] = now   # a mérés ideje (a minták ingestje is ezt állítja)
        seen = []
        for signal in signals:
            key = pattern_key(signal["items"])
            candidate_id = make_candidate_id(signal["diagnostic"], f"signal|{key}")
            seen.append(candidate_id)
            data = self.state["candidates"].get(candidate_id)
            candidate = Candidate.from_dict(data) if data else Candidate(
                candidate_id, signal["diagnostic"], key, first_seen_at=now, kind="signal")
            candidate.stats = {**signal["details"], "subject": signal["subject"], "severity": signal["severity"],
                               "description": signal["description"]}
            candidate.bad_incident_ids = list(signal.get("affected", ()))
            candidate.window_start = candidate.window_end = now
            candidate.last_seen_at = now
            candidate.occurrences.append({"measured_at": now, "severity": signal["severity"]})
            candidate.occurrences = candidate.occurrences[-30:]
            # "most nem": signal_return_days nap után, ha még mindig jelez, visszajön
            if candidate.state == CandidateState.REJECTED_ONCE.value and candidate.rejected_window_end and \
                    measured_at >= datetime.fromisoformat(candidate.rejected_window_end) + timedelta(days=self.signal_return_days):
                candidate.state = CandidateState.QUEUED.value
                candidate.flags = sorted(set(candidate.flags) | {"returned"})
            self._save_candidate(candidate)
        meta = self.state["meta"]
        meta["last_seen"] = sorted(set(meta.get("last_seen", [])) | set(seen))
        self.apply_rules(count_hits=False)
        self.refresh_suggestions()
        return seen

    # ------------------------------------------------------------------
    # Ismert eltérés
    # ------------------------------------------------------------------

    @staticmethod
    def _append_counts(candidate: Candidate, record: Dict[str, Any]) -> Candidate:
        """Egy ablak darabszámai a jelölthöz; ugyanarra az ablakra a régebbi rekordot felváltja.

        Hívja: DiscoveryManager.ingest, DiscoveryManager.record_counts."""
        window = (record["window_start"], record["window_end"])
        candidate.counts = sorted(
            [r for r in candidate.counts if (r["window_start"], r["window_end"]) != window] + [record],
            key=lambda r: r["window_end"],
        )
        return candidate

    def record_counts(self, candidate_id: str, window_start: datetime, window_end: datetime,
                      values: Mapping[str, int]) -> None:
        """Egy korábbi ablak darabszámai (visszamenőleges feltöltés a szokásos eltéréshez).

        Hívja: runner.backfill_counts."""
        record = {"measured_at": self.now, "window_start": window_start.isoformat(),
                  "window_end": window_end.isoformat(), **values}
        self._save_candidate(self._append_counts(self.candidate(candidate_id), record))

    def baseline_progress(self, candidate: Candidate) -> Dict[str, int]:
        """Mennyi múltbeli eset van meg a saját normához (nem átfedő ablakokban) a szükségesből.

        Hívja: DiscoveryService.candidate_dto."""
        windows = non_overlapping(candidate.counts)
        return {"size": sum(r["size"] for r in windows), "windows": len(windows), "needed": int(self.baseline_min_size)}

    def _exceeds_baseline(self, candidate: Candidate, record: Mapping[str, Any]) -> bool:
        """Az ismert eltérésként kezelt jelölt most rosszabb-e a saját normájánál (szignifikánsan).

        Hívja: DiscoveryManager.ingest."""
        comparison = compare_to_baseline(record, candidate.baseline["lift"])
        candidate.stats["baseline_comparison"] = comparison
        return bool(
            comparison
            and record["bad"] >= self.baseline_min_bad
            and comparison["ratio"] >= self.baseline_min_ratio
            and comparison["p_value"] <= self.baseline_max_p
        )

    # ------------------------------------------------------------------
    # Szabályok
    # ------------------------------------------------------------------

    def apply_rules(self, count_hits: bool = True) -> List[str]:
        """A döntésre váró jelöltek láthatóságának újraszámolása a szabályok alapján.

        Elsőbbség: kiemelés > elvetés; az áttörő erősségű (extreme_strength) jelöltet elvetés nem rejti el.
        

        Hívja: DiscoveryManager.add_fact, DiscoveryManager.add_rule, DiscoveryManager.decide, DiscoveryManager.ingest,
        DiscoveryManager.ingest_signals, DiscoveryManager.revoke_fact, DiscoveryManager.revoke_rule,
        DiscoveryService._suppress, learning_sim._decide."""
        now = self.now
        facts = self.facts()
        rules = [rule for rule in self.rules() if rule.is_active(now)]
        hit_rules: Dict[str, int] = {}
        filtered = []
        for candidate in self.candidates():
            if candidate.state not in _OPEN_STATES:
                continue
            matches = [(rule, match) for rule in rules if (match := match_rule(rule, candidate, facts, now))]
            boosts = [match for rule, match in matches if rule.action == RuleAction.BOOST.value]
            drops = [match for rule, match in matches if rule.action == RuleAction.DROP.value]
            hard = any(rule.hard for rule, _ in matches if rule.action == RuleAction.DROP.value)
            candidate.boosted_by = boosts[0] if boosts else None
            candidate.filtered_by = None
            candidate.flags = [flag for flag in candidate.flags if flag != "extreme_override"]
            candidate.state = CandidateState.QUEUED.value
            if drops and not boosts:
                if self.strength(candidate) >= self.extreme_strength and not hard:
                    candidate.flags.append("extreme_override")
                else:
                    candidate.state = CandidateState.FILTERED.value
                    candidate.filtered_by = drops[0]
                    filtered.append(candidate.candidate_id)
                    hit_rules[drops[0]["rule_id"]] = hit_rules.get(drops[0]["rule_id"], 0) + 1
            self._save_candidate(candidate)
        if count_hits:
            for rule_id, hits in hit_rules.items():
                rule = Rule.from_dict(self.state["rules"][rule_id])
                rule.hits += hits
                rule.last_hit_at = now
                self._save_rule(rule)
        return filtered

    def add_fact(self, predicate: str, args: Dict[str, str], valid_until: str = "", note: str = "") -> Fact:
        """Tény felvétele; ha a tény elvető jellegű (pl. nem_dolgozik), a hozzá tartozó tudásszabály is létrejön, és a
        jelöltek láthatósága újraszámolódik.

        Hívja: DiscoveryCli.cmd_available, DiscoveryCli.cmd_fact, DiscoveryManager.decide,
        DiscoveryService.availability, DiscoveryService.close_issue_with_lesson, DiscoveryService.fact,
        DiscoveryService.schedule_action."""
        fact, created = self.knowledge.add_fact(predicate, args, self.now, valid_until, note)
        if not created:
            return fact
        if PREDICATES[predicate].kind == "drop":
            self._ensure_knowledge_rule(predicate)
        self.apply_rules(count_hits=False)
        self.refresh_suggestions()
        return fact

    def knowledge_view(self) -> KnowledgeView:
        """A most érvényes tények (a diagnosztikák mérési kontextusához).

        Hívja: DiscoveryCli._finding_context, DiscoveryCli.cmd_review, DiscoveryService.knowledge_overview,
        learning_sim.run_learning_sim, runner.backfill_counts, runner.run_discovery."""
        return self.knowledge.view(self.now)

    def _ensure_knowledge_rule(self, predicate: str) -> Rule:
        """Predikátumonként egy általános tudásszabály (pl. nem_dolgozik -> az időfüggő minták elvetése).

        Hívja: DiscoveryManager.add_fact, DiscoveryManager.decide."""
        for rule in self.rules():
            if rule.kind == RuleKind.KNOWLEDGE.value and (rule.fact_binding or {}).get("predicate") == predicate \
                    and rule.state == RuleState.ACTIVE.value:
                return rule
        definition = PREDICATES[predicate]
        rule = Rule(
            self._next_id("RULE"), RuleKind.KNOWLEDGE.value, RuleAction.DROP.value,
            fact_binding={"predicate": predicate, "bind": {arg: dim for arg, dim in definition.args}},
            measure_filter={"mode": "time_sensitive"} if predicate == NEM_DOLGOZIK.name else {"mode": "any"},
            created_at=self.now, source="knowledge", reason=definition.description,
        )
        self._save_rule(rule)
        return rule

    def add_rule(self, conditions: List[Dict[str, Any]], action: str = RuleAction.DROP.value,
                 measures: Optional[List[str]] = None, reason: str = "", source: str = "manual",
                 kind: str = RuleKind.REJECTION.value, valid_until: str = "", hard: bool = False) -> Rule:
        """Elvető (vagy kiemelő) szabály felvétele mintaelemekre, opcionálisan mérésekre szűkítve; a kemény szabály az
        áttörő erősségű jelöltet is rejti. Utána a láthatóság és a tipp újraszámolódik.

        Hívja: DiscoveryCli.cmd_rule, DiscoveryManager._rule_from_candidate, DiscoveryManager.decide,
        DiscoveryService._suppress, DiscoveryService.close_issue_with_lesson, DiscoveryService.generalize,
        learning_sim._day, learning_sim._suppress_elements."""
        rule = Rule(
            self._next_id("RULE"), kind, action, conditions=conditions,
            measure_filter={"mode": "list", "measures": measures} if measures else {"mode": "any"},
            created_at=self.now, valid_until=valid_until, source=source, reason=reason, hard=hard,
        )
        self._save_rule(rule)
        self.apply_rules(count_hits=False)
        self.refresh_suggestions()
        return rule

    def confirm_rule(self, rule_id: str) -> Rule:
        """Az elemző megerősíti a szabályt (a felülvizsgálat ettől számol).

        Hívja: DiscoveryCli.cmd_rule, DiscoveryService.rule."""
        rule = Rule.from_dict(self.state["rules"][rule_id])
        rule.reviewed_at = self.now
        self._save_rule(rule)
        return rule

    def revoke_rule(self, rule_id: str, reason: str = "") -> Rule:
        """Szabály visszavonása (a verzió nő, az előzmény megmarad); az általa elvetett jelöltek újra döntésre várnak.

        Hívja: DiscoveryCli.cmd_rule, DiscoveryService.rule."""
        rule = Rule.from_dict(self.state["rules"][rule_id])
        rule.history.append({k: v for k, v in rule.to_dict().items() if k != "history"})
        rule.state, rule.version = RuleState.REVOKED.value, rule.version + 1
        rule.reason = reason or rule.reason
        self._save_rule(rule)
        self.reopen_rejected_by_revoked_rules()
        self.apply_rules(count_hits=False)
        self.refresh_suggestions()
        return rule

    def reopen_rejected_by_revoked_rules(self) -> List[str]:
        """A visszavont szabállyal elvetett jelöltek újra döntésre várnak (ha azóta nem döntöttek róluk
        másként): a szabály visszavonása a döntést is visszavonja. Ismételten hívható (pl. betöltéskor).

        Hívja: DiscoveryManager.revoke_rule, DiscoveryService.__init__."""
        revoked = {rule.rule_id for rule in self.rules() if rule.state == RuleState.REVOKED.value}
        if not revoked:
            return []
        latest: Dict[str, Decision] = {}
        for decision in self.decisions():
            latest[decision.candidate_id] = decision
        reopened = []
        for candidate_id, decision in latest.items():
            if decision.rule_id not in revoked or candidate_id not in self.state["candidates"]:
                continue
            candidate = self.candidate(candidate_id)
            if candidate.state != CandidateState.REJECTED.value:
                continue
            candidate.state = CandidateState.QUEUED.value
            candidate.filtered_by = None
            candidate.flags = sorted(set(candidate.flags) | {"rule_revoked"})
            self._save_candidate(candidate)
            reopened.append(candidate_id)
        return reopened

    def revoke_fact(self, fact_id: str) -> Fact:
        """Tény visszavonása; a jelöltek láthatósága újraszámolódik.

        Hívja: DiscoveryCli.cmd_fact, DiscoveryService.fact, DiscoveryService.schedule_action."""
        fact = self.knowledge.revoke_fact(fact_id)
        self.apply_rules(count_hits=False)
        self.refresh_suggestions()
        return fact

    def covered_by(self, rule_id: str) -> List[Candidate]:
        """Mit takar ez a szabály: az általa elrejtett jelöltek.

        Hívja: DiscoveryCli.cmd_hidden."""
        return [c for c in self.candidates() if (c.filtered_by or {}).get("rule_id") == rule_id]

    # ------------------------------------------------------------------
    # Tipp (hasonlóság alapú) és sorrend
    # ------------------------------------------------------------------

    @staticmethod
    def _features(candidate: Candidate) -> set:
        """A jelölt jellemzői a tipp hasonlóságához (mérés, mezőértékek, mezők).

        Hívja: DiscoveryManager.suggest."""
        features = {f"measure={candidate.measure}"}
        for description in candidate.descriptions():
            for dim, value in description:
                features.add(f"{dim}={value}")
                features.add(f"{dim}=*")
        return features

    def _labelled(self) -> List[Tuple[Candidate, str, float]]:
        """Jelöltenként a legutóbbi tanuló döntés és annak súlya.

        Hívja: DiscoveryManager.refresh_suggestions, DiscoveryManager.suggest."""
        latest: Dict[str, Tuple[str, float]] = {}
        for decision in self.decisions():
            weight = _SCOPE_WEIGHTS.get(decision.scope, 1.0)
            if decision.verdict in _LEARNING_VERDICTS and not decision.auto and weight > 0:
                latest[decision.candidate_id] = (decision.verdict, weight)
        return [(self.candidate(cid), verdict, weight) for cid, (verdict, weight) in latest.items()
                if cid in self.state["candidates"]]

    def suggest(self, candidate: Candidate, labelled: Optional[List[Tuple[Candidate, str, float]]] = None,
                k: int = 5, min_similarity: float = 0.2) -> Optional[Dict[str, Any]]:
        """A leginkább hasonló, már eldöntött jelöltek súlyozott többségi döntése, indoklással.

        A szavazat súlya a hasonlóság és a döntés súlyának szorzata (az egyszeri elvetés gyengébb).
        

        Hívja: DiscoveryManager.refresh_suggestions."""
        labelled = self._labelled() if labelled is None else labelled
        features = self._features(candidate)
        scored = []
        for other, verdict, weight in labelled:
            if other.candidate_id == candidate.candidate_id:
                continue
            other_features = self._features(other)
            similarity = len(features & other_features) / len(features | other_features)
            if similarity >= min_similarity:
                scored.append((similarity, other, verdict, weight))
        if not scored:
            return None
        scored.sort(key=lambda item: -item[0] * item[3])
        top = scored[:k]
        weights: Dict[str, float] = {}
        for similarity, _, verdict, weight in top:
            weights[verdict] = weights.get(verdict, 0.0) + similarity * weight
        verdict = max(weights, key=weights.get)
        share = weights[verdict] / sum(weights.values())
        confidence = round(share * max(s * w for s, _, v, w in top if v == verdict), 3)
        reasons = [f"{other.measure}: {other.key} ({v}{', egyszeri' if w < 1 else ''}, hasonlóság {s:.2f})"
                   for s, other, v, w in top[:3]]
        return {"verdict": verdict, "confidence": confidence, "reasons": reasons}

    # ------------------------------------------------------------------
    # Almérések (child diagnostic): a tudástárban élnek, mert tanított tudás (a reset nem törli)
    # ------------------------------------------------------------------

    def _children_store(self) -> Dict[str, Any]:
        """Az almérések tárolója a tudástárban (a reset nem törli).

        Hívja: DiscoveryManager._child, DiscoveryManager._learn_from_children, DiscoveryManager._save_child,
        DiscoveryManager.add_child, DiscoveryManager.children, DiscoveryManager.revoke_child,
        DiscoveryManager.store_child_results."""
        return self.knowledge.store.setdefault("children", {})

    def children(self, active_only: bool = False) -> List[ChildDef]:
        """Az almérések (csak az aktívak, ha kérik).

        Hívja: DiscoveryManager.auto_decide_by_children, DiscoveryManager.matching_children,
        DiscoveryManager.probe_feedback, DiscoveryService._generalizations, DiscoveryService._name_children,
        DiscoveryService._run_children, DiscoveryService._run_probes, DiscoveryService.children_overview,
        DiscoveryService.overview és mások."""
        result = [ChildDef.from_dict(d) for d in self._children_store().values()]
        return [c for c in result if c.state == "active"] if active_only else result

    def add_child(self, name: str, expression: str, role: str = "explain", threshold: float = 0.6,
                  trigger: Optional[Mapping[str, Any]] = None, note: str = "", source: str = "",
                  as_dimension: bool = False, auto: bool = False, method: str = "check") -> ChildDef:
        """Új almérés (ellenőrzés vagy csoportosítás) egy kiváltóval, szereppel és küszöbbel; a kifejezést előbb
        lefordítja (hibás kifejezésből nem lesz almérés).

        Hívja: DiscoveryManager.probe_feedback, DiscoveryService.children_action."""
        compile_expression(expression)
        if method not in ("check", "group"):
            raise ValueError("Az almérés fajtája: check (ellenőrzés) | group (csoportosítás).")
        if method == "group" and auto:
            raise ValueError("Csoportosító almérés nem dönthet helyetted.")
        if role not in CHILD_ROLES:
            raise ValueError(f"Ismeretlen szerep: {role} (explain | alert).")
        if not str(name).strip():
            raise ValueError("Az almérésnek nevet kell adni.")
        if auto and role != "explain":
            raise ValueError("Automatikus döntés csak magyarázat szerepű almérésnél lehet.")
        threshold = float(threshold)
        if not 0 < threshold <= 1:
            raise ValueError("A küszöb 0 és 1 közötti arány (pl. 0.6).")
        counters = self.knowledge.store.setdefault("meta", {}).setdefault("counters", {})
        counters["CHD"] = counters.get("CHD", 0) + 1
        child = ChildDef(f"CHD-{counters['CHD']:04d}", str(name).strip(), str(expression).strip(), role, threshold,
                         dict(trigger or {}), note, created_at=self.now, source=source, as_dimension=as_dimension, auto=auto,
                         method=method)
        self._children_store()[child.child_id] = child.to_dict()
        self.knowledge.save()
        return child

    def _child(self, child_id: str) -> ChildDef:
        """Egy almérés az azonosítója szerint.

        Hívja: DiscoveryManager.attach_child, DiscoveryManager.child_method_feedback, DiscoveryManager.confirm_child,
        DiscoveryManager.set_child_auto, DiscoveryManager.set_child_dimension, DiscoveryManager.set_child_role."""
        if child_id not in self._children_store():
            raise ValueError(f"Nincs ilyen almérés: {child_id}")
        return ChildDef.from_dict(self._children_store()[child_id])

    def _save_child(self, child: ChildDef) -> ChildDef:
        """Az almérés visszaírása, és a jelöltek tippjének frissítése.

        Hívja: DiscoveryManager.attach_child, DiscoveryManager.child_method_feedback, DiscoveryManager.confirm_child,
        DiscoveryManager.set_child_auto, DiscoveryManager.set_child_dimension, DiscoveryManager.set_child_role."""
        self._children_store()[child.child_id] = child.to_dict()
        self.knowledge.save()
        return child

    def attach_child(self, child_id: str, trigger: Mapping[str, Any]) -> Tuple[ChildDef, bool]:
        """Meglévő almérés újabb helyre: kap egy kiváltót (nem másolódik; a statisztikája közös marad).

        Hívja: DiscoveryService.children_action."""
        child = self._child(child_id)
        if child.state != "active":
            raise ValueError("Visszavont almérés nem rendelhető hozzá.")
        added = child.attach(trigger)
        self._save_child(child)
        return child, added

    def confirm_child(self, child_id: str) -> ChildDef:
        """Az elemző szerint az almérés jó így: a felülvizsgálat a mostani statisztikától számol.

        Hívja: DiscoveryService.children_action."""
        child = self._child(child_id)
        child.reviewed_at, child.reviewed_stats = self.now, dict(child.stats)
        return self._save_child(child)

    def set_child_dimension(self, child_id: str, on: bool) -> ChildDef:
        """Keresési szempont be/ki: az értéke dimenzióként kerül az adatlapokra (a következő feltárástól).

        Hívja: DiscoveryService.children_action."""
        child = self._child(child_id)
        if child.state != "active":
            raise ValueError("Visszavont almérés nem lehet keresési szempont.")
        child.as_dimension = bool(on)
        return self._save_child(child)

    def revoke_child(self, child_id: str) -> ChildDef:
        """Almérés visszavonása; a jelöltekről lekerül az eredménye.

        Hívja: DiscoveryService.children_action."""
        if child_id not in self._children_store():
            raise ValueError(f"Nincs ilyen almérés: {child_id}")
        child = ChildDef.from_dict(self._children_store()[child_id])
        child.state = "revoked"
        self._children_store()[child_id] = child.to_dict()
        for candidate in self.candidates():
            results = candidate.stats.get("children") or []
            if any(r.get("child_id") == child_id for r in results):
                candidate.stats["children"] = [r for r in results if r.get("child_id") != child_id]
                self._save_candidate(candidate)
        self.knowledge.save()
        self.refresh_suggestions()
        return child

    def child_method_feedback(self, child_id: str, candidate_id: str, verdict: str) -> ChildDef:
        """Egy almérés mint bontási módszer ennél a jelöltnél: good = jó módszer (ezentúl minden ilyen fajtájú jelöltnél,
        ugyanannál a mérésnél magától lefut); bad = nem jó (ennél a fajtánál többé nem fut).

        Hívja: DiscoveryManager.probe_feedback, DiscoveryService.children_action."""
        child, candidate = self._child(child_id), self.candidate(candidate_id)
        general = {"kind": candidate.kind, "measure": candidate.measure}
        same = lambda t: t.get("kind") == general["kind"] and t.get("measure") == general["measure"] and not t.get("items") \
            and not t.get("candidate_id")
        stats = child.stats
        if verdict == "good":
            child.excluded = [t for t in child.excluded if not same(t)]
            child.attach(general)
            stats["method_good"] = stats.get("method_good", 0) + 1
        elif verdict == "bad":
            if not any(same(t) for t in child.excluded):
                child.excluded.append(general)
            stats["method_bad"] = stats.get("method_bad", 0) + 1
            results = candidate.stats.get("children") or []
            candidate.stats["children"] = [r for r in results if r.get("child_id") != child_id]
            self._save_candidate(candidate)
        else:
            raise ValueError("A visszajelzés: good | bad")
        candidate = self.candidate(candidate_id)
        candidate.feedback.setdefault("methods", {})[child_id] = verdict   # értékelt: a felületen már nem kér döntést
        self._save_candidate(candidate)
        log = self.knowledge.store.setdefault("method_log", [])
        log.append({"at": self.now, "child_id": child_id, "candidate_id": candidate_id, "verdict": verdict, **general})
        self.knowledge.store["method_log"] = log[-500:]
        return self._save_child(child)

    def excluded_probes(self, candidate: Candidate) -> set:
        """A "nem jó" gyári próbák ennél a jelöltfajtánál (fajta és mérés).

        Hívja: DiscoveryService._run_probes."""
        return {e["probe"] for e in self.knowledge.store.get("probe_excluded", [])
                if e.get("kind") == candidate.kind and e.get("measure") == candidate.measure}

    def probe_feedback(self, candidate_id: str, probe: Mapping[str, Any], verdict: str) -> Optional[ChildDef]:
        """A gép próbálkozására adott visszajelzés. Jó: a gyári próbából almérés lesz (ilyen fajtájú jelöltekre), a tanított
        almérés ezentúl ilyen fajtánál is fut. Nem jó: ennél a fajtánál többé nem próbálja.

        Hívja: DiscoveryService.children_action."""
        candidate = self.candidate(candidate_id)
        key, source = str(probe["probe"]), str(probe.get("source", ""))
        child: Optional[ChildDef] = None
        if source == "gyári":
            if verdict == "good":
                method = str(probe.get("method", "check"))
                existing = next((c for c in self.children(active_only=True)
                                 if c.expression == probe["expression"] and c.method == method), None)
                if existing is not None:
                    child = self.child_method_feedback(existing.child_id, candidate_id, "good")
                else:
                    child = self.add_child(str(probe["name"]), str(probe["expression"]), "explain", 0.6,
                                           {"kind": candidate.kind, "measure": candidate.measure},
                                           note="a gép próbálkozásából", source=f"próba:{key}", method=method)
            elif verdict == "bad":
                excluded = self.knowledge.store.setdefault("probe_excluded", [])
                excluded.append({"probe": key, "kind": candidate.kind, "measure": candidate.measure, "at": self.now})
            else:
                raise ValueError("A visszajelzés: good | bad")
        else:
            child = self.child_method_feedback(key, candidate_id, verdict)
        candidate = self.candidate(candidate_id)
        candidate.stats["probes"] = [p for p in candidate.stats.get("probes") or [] if p.get("probe") != key]
        self._save_candidate(candidate)
        self.knowledge.save()
        return child

    def matching_children(self, candidate: Candidate) -> List[ChildDef]:
        """Az aktív almérések, amelyek erre a jelöltre lefutnak.

        Hívja: DiscoveryService._run_children, DiscoveryService.children_action."""
        return [c for c in self.children(active_only=True) if c.matches(candidate)]

    def store_child_results(self, candidate_id: str, results: List[Dict[str, Any]]) -> None:
        """Az almérések eredménye a jelöltre (az előző aránnyal együtt), és a futási statisztika.

        Hívja: DiscoveryService._run_children."""
        candidate = self.candidate(candidate_id)
        candidate.stats["children"] = with_previous(results, candidate.stats.get("children") or [])
        self._save_candidate(candidate)
        store = self._children_store()
        for result in results:
            if result["child_id"] in store:
                stats = store[result["child_id"]].setdefault("stats", {})
                stats["runs"] = stats.get("runs", 0) + 1
                stats["triggered"] = stats.get("triggered", 0) + int(bool(result["triggered"]))
        self.knowledge.save()

    def set_child_role(self, child_id: str, role: str) -> ChildDef:
        """Az almérés szerepe utólag (pl. a „Jó módszer”-rel létrejött magyarázatból riasztás); riasztás nem dönthet helyetted.

        Hívja: DiscoveryService.children_action."""
        if role not in CHILD_ROLES:
            raise ValueError(f"Ismeretlen szerep: {role} (explain | alert).")
        child = self._child(child_id)
        child.role = role
        if role != "explain":
            child.auto = False
        return self._save_child(child)

    def set_child_auto(self, child_id: str, on: bool) -> ChildDef:
        """Magyarázó almérésnél: ha teljesül, döntsön-e helyettünk („most nem”).

        Hívja: DiscoveryService.children_action."""
        child = self._child(child_id)
        if on and child.role != "explain":
            raise ValueError("Automatikus döntés csak magyarázat szerepű almérésnél lehet.")
        child.auto = bool(on)
        return self._save_child(child)

    def auto_decide_by_children(self) -> List[str]:
        """Döntésre váró jelöltek, amelyeken egy automatikus magyarázat teljesült: "most nem" az elemző helyett
        (nem fogyaszt keretet, nem tanítja a tippet; ha a jelenség megmarad, a szokásos módon visszajön).

        Hívja: DiscoveryService._run_children."""
        auto_children = {c.child_id: c for c in self.children(active_only=True) if c.auto and c.role == "explain"}
        decided = []
        if not auto_children:
            return decided
        for candidate in self._open_pool():
            fired = [r for r in candidate.stats.get("children") or []
                     if r.get("triggered") and r.get("child_id") in auto_children]
            if fired:
                child = auto_children[fired[0]["child_id"]]
                self.decide(candidate.candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value,
                            note=f"almérés: {child.name} ({fired[0]['text']})", auto=child.child_id)
                decided.append(candidate.candidate_id)
        return decided

    @staticmethod
    def _child_suggestion(candidate: Candidate) -> Optional[Dict[str, Any]]:
        """A teljesült almérések tippje: a riasztás finding felé, a magyarázat nem-finding felé visz.

        Hívja: DiscoveryManager.refresh_suggestions."""
        fired = [r for r in candidate.stats.get("children") or [] if r.get("triggered")]
        alerts = [r for r in fired if r["role"] == "alert"]
        explains = [r for r in fired if r["role"] == "explain"]
        if alerts:
            return {"verdict": Verdict.FINDING.value, "confidence": 0.8, "reasons": [r["text"] for r in alerts], "source": "child"}
        if explains:
            return {"verdict": Verdict.NOT_FINDING.value, "confidence": 0.8, "reasons": [r["text"] for r in explains], "source": "child"}
        return None

    def _learn_from_children(self, candidate: Candidate, verdict: str) -> None:
        """Döntéskor: a teljesült almérés jelzésével egyezett-e az elemző (hasznossága a felülvizsgálathoz).

        Hívja: DiscoveryManager.decide."""
        store = self._children_store()
        changed = False
        for result in candidate.stats.get("children") or []:
            if not result.get("triggered") or result.get("child_id") not in store:
                continue
            stats = store[result["child_id"]].setdefault("stats", {})
            stats["decided"] = stats.get("decided", 0) + 1
            agreed = (verdict == Verdict.FINDING.value) == (result["role"] == "alert")
            stats["agreed"] = stats.get("agreed", 0) + int(agreed)
            changed = True
        if changed:
            self.knowledge.save()

    def refresh_suggestions(self) -> None:
        """A döntésre váró jelöltek tippje, lefedettsége, hatóköre és prioritása újraszámolva.

        Hívja: DiscoveryManager.add_fact, DiscoveryManager.add_rule, DiscoveryManager.decide, DiscoveryManager.ingest,
        DiscoveryManager.ingest_signals, DiscoveryManager.revoke_child, DiscoveryManager.revoke_fact,
        DiscoveryManager.revoke_rule, DiscoveryService._attach_to_open_issues és mások."""
        labelled = self._labelled()
        accepted = [c for c in self.candidates() if c.state == CandidateState.ACCEPTED.value]
        pool = self._entries(self._open_pool())
        for candidate in self.candidates():
            if candidate.state != CandidateState.QUEUED.value:
                continue
            candidate.suggestion = self._child_suggestion(candidate) or self.suggest(candidate, labelled)
            candidate.covered_by = self._covering_finding(candidate, accepted)
            candidate.reach = reach(candidate.items, candidate.measure, pool, candidate.candidate_id)
            candidate.priority = self._priority(candidate)
            self._save_candidate(candidate)

    # ------------------------------------------------------------------
    # Döntési csomópontok
    # ------------------------------------------------------------------

    @staticmethod
    def _entries(candidates: Iterable[Candidate]) -> List[Tuple[str, str, List]]:
        """A jelöltek (azonosító, mérés, leírások) alakban a csomópont- és hatókörszámításhoz.

        Hívja: DiscoveryManager.clusters, DiscoveryManager.refresh_suggestions."""
        return [(c.candidate_id, c.measure, c.descriptions()) for c in candidates]

    # ------------------------------------------------------------------
    # Bontási visszajelzés: melyik bontási irány jó út, melyik zsákutca
    # ------------------------------------------------------------------

    def _breakdown_store(self) -> Dict[str, Any]:
        """A bontási tudás tárolója (mezőpreferenciák, mérésenkénti jó szempontok, tiltott utak, napló).

        Hívja: DiscoveryManager.block_breakdown_path, DiscoveryManager.blocked_breakdown_paths,
        DiscoveryManager.breakdown_feedback, DiscoveryManager.breakdown_preferences, DiscoveryManager.good_dimensions,
        DiscoveryManager.reset_breakdown_dimension, DiscoveryService._filtered_breakdown, DiscoveryService.generalize,
        DiscoveryService.knowledge_overview."""
        store = self.knowledge.store.setdefault("breakdown", {})
        store.setdefault("dimensions", {})   # mező -> {"good": n, "hidden": bool}
        store.setdefault("measures", {})     # mérés -> {mező: hányszor jelölted jó szempontnak}
        store.setdefault("paths", {})        # "szülőfajta>bontó mező" -> {"blocked": bool}
        store.setdefault("log", [])
        return store

    def breakdown_preferences(self) -> Dict[str, Dict[str, Any]]:
        """Mezőnként a bontási preferencia (jó szempontnak jelölve hányszor, rejtett-e).

        Hívja: DiscoveryService._filtered_breakdown, DiscoveryService.knowledge_overview."""
        return dict(self._breakdown_store()["dimensions"])

    def good_dimensions(self, measure: str) -> List[str]:
        """A mérésnél bevált szempontok (a „Jó szempont” visszajelzések szerint, a gyakoribb elöl): a gép ezeket minden
        ilyen mérésű jelöltnél megnézi. A „soha” mező nem kerül bele.

        Hívja: DiscoveryService._good_dimension_insights."""
        hidden = {d for d, p in self._breakdown_store()["dimensions"].items() if p.get("hidden")}
        counts = self._breakdown_store()["measures"].get(measure, {})
        return [d for d, _ in sorted(counts.items(), key=lambda x: -x[1]) if d not in hidden]

    def breakdown_feedback(self, parent_id: str, dimension: str, value: str, verdict: str) -> Dict[str, Any]:
        """verdict: good (jó szempont: ennél a mérésnél a gép ezt a mezőt minden jelöltnél megnézi, és a bontásokban
        előrébb kerül – a módszerről szól, nem az értékről) | dead_end (ennél a bontásnál ez a sor ne jöjjön) | never
        (ez a mező egyik bontásban se).

        Hívja: DiscoveryService.breakdown_feedback."""
        parent = self.candidate(parent_id)
        store = self._breakdown_store()
        prefs = store["dimensions"].setdefault(dimension, {"good": 0, "hidden": False})
        if verdict == "good":
            prefs["good"] = prefs.get("good", 0) + 1
            prefs["hidden"] = False
            by_measure = store["measures"].setdefault(parent.measure, {})
            by_measure[dimension] = by_measure.get(dimension, 0) + 1
            done = parent.feedback.setdefault("breakdown_done", [])
            if [dimension, value] not in done:
                done.append([dimension, value])
            self._save_candidate(parent)
        elif verdict == "dead_end":
            hidden = parent.stats.setdefault("breakdown_hidden", [])
            if [dimension, value] not in hidden:
                hidden.append([dimension, value])
            self._save_candidate(parent)
        elif verdict == "never":
            prefs["hidden"] = True
        else:
            raise ValueError("A visszajelzés: good | dead_end | never")
        store["log"].append({"at": self.now, "parent": parent_id, "dimension": dimension, "value": value, "verdict": verdict,
                             "parent_dims": sorted({d for d, _ in parent.items})})
        store["log"] = store["log"][-500:]
        self.knowledge.save()
        return prefs

    def blocked_breakdown_paths(self) -> List[Tuple[str, str]]:
        """A tiltott bontási utak (szülő mezőfajtája → bontó mező).

        Hívja: DiscoveryService._filtered_breakdown, DiscoveryService._refinements,
        DiscoveryService.knowledge_overview, runner.run_discovery."""
        return [tuple(key.split(">", 1)) for key, value in self._breakdown_store()["paths"].items() if value.get("blocked")]

    def block_breakdown_path(self, parent_class: str, child_dimension: str, blocked: bool = True) -> None:
        """Egy bontási út tiltása vagy feloldása.

        Hívja: DiscoveryService.breakdown_feedback, DiscoveryService.generalize."""
        paths = self._breakdown_store()["paths"]
        if blocked:
            paths[f"{parent_class}>{child_dimension}"] = {"blocked": True, "at": self.now}
        else:
            paths.pop(f"{parent_class}>{child_dimension}", None)
        self.knowledge.save()

    def reset_breakdown_dimension(self, dimension: str) -> None:
        """Egy mező bontási preferenciáinak törlése (minden mérésnél).

        Hívja: DiscoveryService.breakdown_feedback."""
        store = self._breakdown_store()
        store["dimensions"].pop(dimension, None)
        for counts in store["measures"].values():
            counts.pop(dimension, None)
        self.knowledge.save()

    def contexts(self) -> Dict[str, List[Tuple[Tuple[str, str], ...]]]:
        """Mérésenként a tovább bontott minták (minden leírásukkal): ezeken belül a bányászat a kontextushoz mér.

        Hívja: runner.run_discovery."""
        result: Dict[str, List[Tuple[Tuple[str, str], ...]]] = {}
        for candidate in self.candidates():
            if candidate.state == CandidateState.REFINED.value and candidate.kind == "contrast":
                result.setdefault(candidate.measure, []).extend(candidate.descriptions())
        return result

    def unrefine(self, candidate_id: str) -> Candidate:
        """A továbbbontás visszavonása: a minta újra önmagában is jelölt (döntésre vár).

        Hívja: DiscoveryService.unrefine."""
        candidate = self.candidate(candidate_id)
        if candidate.state != CandidateState.REFINED.value:
            raise ValueError(f"A(z) {candidate_id} nincs tovább bontva.")
        candidate.state = CandidateState.QUEUED.value
        candidate.flags = sorted(set(candidate.flags) | {"returned"})
        self._save_candidate(candidate)
        return candidate

    @staticmethod
    def is_question(candidate: Candidate) -> bool:
        """Elérhetőségi kérdés (a jelzés csak a válasz után dönthető el): nem a döntési keret része.

        Hívja: DiscoveryManager._open_pool, DiscoveryManager.questions."""
        return candidate.kind == "signal" and candidate.stats.get("availability") == "unknown"

    def _waiting(self) -> List[Candidate]:
        """A döntésre váró jelöltek: a legutóbbi feltárásban látottak, és a különleges okból visszatértek (romlott a
        normájához, szabály visszavonva, riasztó almérés).

        Hívja: DiscoveryManager._open_pool, DiscoveryManager.questions."""
        last_seen = set(self.state["meta"].get("last_seen", []))
        return [
            c for c in self.candidates()
            if c.state == CandidateState.QUEUED.value
            and (c.candidate_id in last_seen or {"exceeds_baseline", "rule_revoked", "child_alert"} & set(c.flags))
        ]

    def _open_pool(self) -> List[Candidate]:
        """A döntésre váró jelöltek (a jelöltsor merítése, keret nélkül), a nyitott kérdések nélkül.

        Hívja: DiscoveryManager.active_candidates, DiscoveryManager.attach_to_open_issues,
        DiscoveryManager.auto_decide_by_children, DiscoveryManager.clusters, DiscoveryManager.queue,
        DiscoveryManager.refresh_suggestions, DiscoveryService._brief_queue, DiscoveryService._method_effect,
        DiscoveryService._waiting_ids."""
        return [c for c in self._waiting() if not self.is_question(c)]

    def active_candidates(self) -> List[Candidate]:
        """A döntésre váró és a tovább bontott (vizsgálat alatt álló) jelöltek: ezeken futnak az almérések, a gép
        próbálkozásai és a bevált szempontok. Hívja: DiscoveryService (almérések, módszer- és bontási visszajelzés)."""
        return self._open_pool() + [c for c in self.candidates() if c.state == CandidateState.REFINED.value]

    def questions(self) -> List[Candidate]:
        """A megválaszolandó elérhetőségi kérdések (a válasz tény, nem döntés).

        Hívja: DiscoveryService.overview."""
        return sorted((c for c in self._waiting() if self.is_question(c)), key=lambda c: (-c.priority, c.key))

    def clusters(self, limit: int = 15) -> List[Dict[str, Any]]:
        """Közös részminták a döntésre váró jelöltekben: egy döntéssel több jelölt is eldönthető.

        Hívja: DiscoveryService.overview."""
        pool = self._open_pool()
        general = {c.items: c.candidate_id for c in pool}
        result = []
        for cluster in find_clusters(self._entries(pool))[:limit]:
            items = tuple(tuple(item) for item in cluster["items"])
            # ha a részminta maga is jelölt (pl. "G"), az a természetes döntési pont
            cluster["candidate_id"] = next((general[(item,)] for item in items if (item,) in general), None)
            result.append(cluster)
        return result

    @staticmethod
    def _covering_finding(candidate: Candidate, accepted: List[Candidate], share: float = 0.8) -> Optional[str]:
        """Egy elfogadott finding, amely ugyanarra a mérésre lefedi a jelöltet: a jelölt a finding
        szűkítése (bármelyik leírása szerint), vagy a rossz ticketjei nagyrészt a findingéi.

        Hívja: DiscoveryManager.attach_to_open_issues, DiscoveryManager.refresh_suggestions."""
        bad = set(candidate.bad_incident_ids)
        for finding in accepted:
            if finding.measure != candidate.measure or finding.candidate_id == candidate.candidate_id:
                continue
            finding_items = set(finding.items)
            if any(finding_items <= set(with_implied(tuple(sorted(d)))) for d in candidate.descriptions()):
                return finding.key
            if bad and len(bad & set(finding.bad_incident_ids)) >= share * len(bad):
                return finding.key
        return None

    def attach_to_open_issues(self, open_issue_ids: set) -> List[Tuple[str, str]]:
        """A döntésre váró jelöltek, amelyeket egy nyitott issue-jú elfogadott finding lefed (szűkítése, vagy az érintett
        esetei nagyrészt a findingéi): az issue-hoz csatolódnak, nem kerülnek a sorba. Ha az issue lezárul, a
        csatolás feloldódik (ld. release_attached).

        Hívja: DiscoveryService._attach_to_open_issues, learning_sim._day."""
        accepted = {c.key: c for c in self.candidates() if c.state == CandidateState.ACCEPTED.value and c.issue_id in open_issue_ids}
        if not accepted:
            return []
        attached = []
        for candidate in self._open_pool():
            key = self._covering_finding(candidate, list(accepted.values()))
            if key is None:
                continue
            finding = accepted[key]
            candidate.state = CandidateState.ATTACHED.value
            candidate.attached_to = finding.issue_id
            self._save_candidate(candidate)
            attached.append((candidate.candidate_id, finding.issue_id))
        return attached

    def release_attached(self, open_issue_ids: set) -> List[str]:
        """A már nem nyitott issue-hoz csatolt jelöltek újra döntésre várnak (ha még fennállnak).

        Hívja: DiscoveryService._attach_to_open_issues, learning_sim._close_issues."""
        released = []
        for candidate in self.candidates():
            if candidate.state == CandidateState.ATTACHED.value and candidate.attached_to not in open_issue_ids:
                candidate.state = CandidateState.QUEUED.value
                candidate.attached_to = None
                self._save_candidate(candidate)
                released.append(candidate.candidate_id)
        return released

    def link_issue(self, candidate_id: str, issue_id: Optional[str]) -> None:
        """A jelölthöz rendeli a belőle nyílt issue-t (a csatoláshoz és az ismert hibához).

        Hívja: DiscoveryCli.cmd_finding, DiscoveryService.decide, learning_sim._decide."""
        candidate = self.candidate(candidate_id)
        candidate.issue_id = issue_id
        self._save_candidate(candidate)

    @staticmethod
    def strength(candidate: Candidate) -> float:
        """Statisztikai erősség: -log10(q), 30-nál vágva.

        Hívja: DiscoveryManager._priority, DiscoveryManager.apply_rules, DiscoveryService._strong_under_rules."""
        return min(-math.log10(max(candidate.stats.get("q_value", 1.0), 1e-300)), 30.0)

    _SIGNAL_PRIORITY = {"EXCEEDED_EXTREME": 8.0, "EXCEEDED_MODERATE": 4.0}

    def _priority(self, candidate: Candidate) -> float:
        # a jelzésnek nincs statisztikai erőssége: a diagnosztika súlyossága adja az alapot
        """A jelölt sorrendje: erősség (jelzésnél a súlyosság), kiemelés, visszatérő „most nem”, lefedettség, hatókör,
        kiemelt mező és a tipp.

        Hívja: DiscoveryManager.refresh_suggestions."""
        priority = self._SIGNAL_PRIORITY.get(str(candidate.stats.get("severity")), 2.0) if candidate.kind == "signal" \
            else self.strength(candidate)
        if candidate.boosted_by:
            priority += 10
        # minden korábbi "most nem" után előrébb: ami mindig visszajön, azzal foglalkozni kell
        priority += self.not_now_boost * candidate.not_now
        if candidate.covered_by:
            priority *= 0.5  # egy elfogadott finding már nagyrészt elmondja; hátrébb, de nem rejtve
        # a döntése a szűkítéseit is eldönti: előrébb (szűkítésenként 2, legfeljebb 10)
        priority += min(10.0, 2.0 * len(candidate.reach))
        # kiemelt mezőre (pl. service) vonatkozó jelölt: előrébb
        if self.emphasized_dimensions and any(dim in self.emphasized_dimensions for dim, _ in candidate.items):
            priority += 3.0
        if candidate.suggestion:
            factor = 0.5 * candidate.suggestion["confidence"]
            priority *= (1 + factor) if candidate.suggestion["verdict"] == Verdict.FINDING.value else (1 - factor)
        return round(priority, 3)

    def capacity(self) -> Dict[str, Any]:
        """A mai döntési keret: betanítási fázisban (amíg kevés a tanuló döntés) nagyobb, utána tartós.

        A mai napot a mérés (virtuális) ideje adja; egy csomópont-döntés egy döntésnek számít.
        

        Hívja: DiscoveryService.overview, learning_sim._day."""
        learning = sum(1 for d in self.decisions() if d.verdict in _LEARNING_VERDICTS and not d.auto)
        training = learning < self.training_decisions
        today = self.now[:10]
        used = sum(1 for d in self.decisions() if today and d.at[:10] == today and not d.auto)
        total = self.training_capacity if training else self.decision_capacity
        auto_today = sum(1 for d in self.decisions() if today and d.at[:10] == today and d.auto)
        return {"phase": "training" if training else "steady", "capacity": total, "used_today": used, "auto_today": auto_today,
                "remaining": max(0, total - used), "learning_decisions": learning,
                "training_decisions": self.training_decisions}

    def queue(self, limit: int = 20, decisions: Optional[int] = None) -> List[Candidate]:
        """A döntésre váró jelöltek: a legutóbbi futásban látottak és a figyelésből visszatértek.

        `decisions`: legfeljebb ennyi elemzői döntésnyi jelölt, prioritás szerint; a többi a következő napokon kerül
        elő. Minden jelölt egy döntés: a családokon nincs közös művelet, a tagokról egyenként döntesz.
        

        Hívja: DiscoveryCli.cmd_queue, DiscoveryService.overview, learning_sim._day."""
        items = self._open_pool()
        items.sort(key=lambda c: (-c.priority, c.key))
        if decisions is not None:
            items = items[:max(0, decisions)]
        return items[:limit]

    # ------------------------------------------------------------------
    # Döntések
    # ------------------------------------------------------------------

    def decide(
        self,
        candidate_id: str,
        verdict: str,
        scope: Optional[str] = None,
        note: str = "",
        fact: Optional[Tuple[str, Dict[str, str]]] = None,
        rule_conditions: Optional[List[Dict[str, Any]]] = None,
        rule_measures: Optional[List[str]] = None,
        auto: str = "",
    ) -> Decision:
        """Egy döntés rögzítése a jelöltre (feladat, most nem, bontás, elvetés szabállyal vagy ténnyel, ismert
        eltérés), az állapot és a tanulás frissítésével.

        Hívja: DiscoveryCli.cmd_finding, DiscoveryCli.cmd_known, DiscoveryCli.cmd_reject,
        DiscoveryManager.auto_decide_by_children, DiscoveryService._suppress,
        DiscoveryService.close_issue_with_lesson, DiscoveryService.decide, learning_sim._close_issues,
        learning_sim._decide."""
        candidate = self.candidate(candidate_id)
        decision = Decision(
            self._next_id("DEC"), candidate_id, self.now, verdict, scope, note=note,
            suggestion_snapshot=candidate.suggestion, stats_snapshot=dict(candidate.stats), auto=auto,
        )
        candidate.flags = [flag for flag in candidate.flags
                           if flag not in {"returned", "exceeds_baseline", "rule_revoked", "child_alert"}]
        if not auto:   # az almérés saját döntése nem igazolhatja önmagát
            self._learn_from_children(candidate, verdict)

        if verdict == Verdict.FINDING.value:
            candidate.state = CandidateState.ACCEPTED.value
        elif verdict == Verdict.KNOWN_DEVIATION.value:
            # Emberi döntés: a szokásos eltérés itt fagy be, és utána magától nem követi a mintát
            # (különben egy lassan romló csoport sosem jelezne)
            # Kevés múltbeli adatnál a döntés akkor is érvényes: a jelölt kikerül a sorból, és a norma
            # magától befagyasztódik, amint a napi futások összegyűjtik a szükséges adatot (ld. ingest)
            candidate.baseline = freeze_baseline(candidate.counts, self.now, self.baseline_min_size, self.baseline_prior)
            candidate.state = CandidateState.BASELINED.value
            if candidate.baseline is None:
                candidate.flags = sorted(set(candidate.flags) | {"baseline_pending"})
        elif verdict == Verdict.REFINE.value:
            # A minta önmagában nem hír (pl. szombaton lassabb a felvétel), de nem is elvetett: kontextus lesz,
            # és csak a benne, hozzá képest is kiugró szűkítései jönnek elő (ld. mining.mine contexts)
            # Jelzésnél és trendnél nincs kontextus-bányászat: a bontás a vizsgálat helye (almérések, módszerek)
            candidate.state = CandidateState.REFINED.value
        elif verdict == Verdict.NOT_FINDING.value:
            if scope == Scope.THIS_OCCURRENCE.value:
                candidate.state = CandidateState.REJECTED_ONCE.value
                candidate.rejected_window_end = candidate.window_end
                if not auto:
                    candidate.not_now += 1
            elif scope == Scope.THIS_PATTERN.value:
                rule = self._rule_from_candidate(candidate, f"decision:{decision.decision_id}")
                decision.rule_id = rule.rule_id
                candidate.state = CandidateState.REJECTED.value
            elif scope == Scope.RULE.value:
                if fact:
                    created = self.add_fact(fact[0], fact[1], note=note)
                    decision.fact_id = created.fact_id
                    if PREDICATES[fact[0]].kind == "drop":
                        decision.rule_id = self._ensure_knowledge_rule(fact[0]).rule_id
                elif rule_conditions:
                    decision.rule_id = self.add_rule(rule_conditions, measures=rule_measures, reason=note,
                                                     source=f"decision:{decision.decision_id}").rule_id
                else:
                    raise ValueError("Szabályos elvetéshez tény vagy feltétel szükséges.")
                candidate.state = CandidateState.REJECTED.value
            else:
                raise ValueError(f"Ismeretlen hatókör: {scope}")
        else:
            raise ValueError(f"Ismeretlen döntés: {verdict}")

        self._save_candidate(candidate)
        self.state["decisions"].append(decision.to_dict())
        self.apply_rules(count_hits=False)
        self.refresh_suggestions()
        return decision

    def _rule_from_candidate(self, candidate: Candidate, source: str) -> Rule:
        """'Erre a mintára mindig': kizáró szabály a pontos mintára (és így a szűkítéseire is).

        Hívja: DiscoveryManager.decide."""
        conditions = [{"dim": dim, "values": [value]} for dim, value in candidate.items]
        return self.add_rule(conditions, measures=[candidate.measure], source=source,
                             reason=f"Elvetett minta: {candidate.key}")

    # ------------------------------------------------------------------
    # Mutatók
    # ------------------------------------------------------------------

    def learning_stats(self) -> Dict[str, Any]:
        """A tanulás mérőszámai a döntésnaplóból: döntések száma és a tipp egyetértési aránya.

        Hívja: DiscoveryCli.cmd_stats, DiscoveryService.overview."""
        decisions = [d for d in self.decisions() if d.verdict in _LEARNING_VERDICTS and not d.auto]
        with_tip = [d for d in decisions if d.suggestion_snapshot]
        agreed = [d for d in with_tip if d.suggestion_snapshot["verdict"] == d.verdict]
        return {
            "decisions": len(self.state["decisions"]),
            "learning_decisions": len(decisions),
            "with_suggestion": len(with_tip),
            "agreement": round(len(agreed) / len(with_tip), 3) if with_tip else None,
            "filtered_now": sum(1 for c in self.candidates() if c.state == CandidateState.FILTERED.value),
        }
