from dataclasses import replace
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from model.knowledge.fact import Fact
from model.knowledge.group_schedules import GroupSchedules
from model.knowledge.knowledge_view import KnowledgeView
from model.knowledge.predicates import CHILD_DIMENSION_PREFIX, ELERHETO, NEM_ELERHETO, PREDICATES, SZINT
from model.knowledge.rule_state import RuleState


class KnowledgeBase:
    """A tények tára. `path` nélkül csak a megadott szótárban (pl. egy másik állapot részeként) él."""

    def __init__(self, store: Optional[Dict[str, Any]] = None, path: Optional[str] = None):
        """A közös tudástár (fájlból, vagy tesztben memóriában).

        Létrehozza: analyzer, discovery_cli, synthetic.learning_sim, learning.discovery_manager."""
        self.path = Path(path) if path else None
        self.store: Dict[str, Any] = store if store is not None else {}
        if self.path is not None:
            self.reload()
        self._ensure()

    def _ensure(self) -> None:
        """A tár alapszerkezete (hiányzó részek létrehozása).

        Hívja: KnowledgeBase.__init__, KnowledgeBase.reload."""
        self.store.setdefault("facts", {})
        self.store.setdefault("meta", {}).setdefault("counters", {})

    def reload(self) -> None:
        """A fájl aktuális tartalma (más folyamat, pl. a konzol írhatta közben).

        Hívja: DiscoveryService.backup_action, IncidentAnalyzer._knowledge_view, KnowledgeBase.__init__."""
        if self.path is not None and self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self.store.clear()
            self.store.update(data)
        self._ensure()

    def save(self) -> None:
        """A tudástár mentése (atomikusan).

        Hívja: DiscoveryManager._learn_from_children, DiscoveryManager._save_child, DiscoveryManager.add_child,
        DiscoveryManager.block_breakdown_path, DiscoveryManager.breakdown_feedback, DiscoveryManager.probe_feedback,
        DiscoveryManager.reset_breakdown_dimension, DiscoveryManager.revoke_child,
        DiscoveryManager.store_child_results és mások."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.store, ensure_ascii=False, indent=1), encoding="utf-8")
        temp.replace(self.path)

    # --- olvasás ---

    def facts(self) -> List[Fact]:
        """Az összes tény.

        Hívja: DiscoveryCli.cmd_review, DiscoveryManager.facts, DiscoveryService.knowledge_overview,
        DiscoveryService.review, DiscoveryService.schedule_action, KnowledgeBase.active_facts."""
        return [Fact.from_dict(data) for data in self.store["facts"].values()]

    def fact(self, fact_id: str) -> Fact:
        """Egy tény az azonosítója szerint.

        Hívja: KnowledgeBase.confirm_fact, KnowledgeBase.revoke_fact."""
        return Fact.from_dict(self.store["facts"][fact_id])

    def active_facts(self, now: str = "") -> List[Fact]:
        """Az adott pillanatban érvényes tények.

        Hívja: KnowledgeBase.add_fact, KnowledgeBase.view."""
        return [fact for fact in self.facts() if fact.is_active(now)]

    def view(self, now: str = "") -> KnowledgeView:
        """A mérés pillanatában érvényes tudás a diagnosztikáknak: szintek, elérhetőség, keresési szempontok
        (almérések), munkaidők, szűkebb küszöbök.

        Hívja: DiscoveryManager.knowledge_view, DiscoveryService._brief_queue, DiscoveryService._profiles,
        DiscoveryService._run_children, DiscoveryService._run_probes, DiscoveryService._unit_sets,
        DiscoveryService.analysis, DiscoveryService.backlog_calendar, DiscoveryService.children_action és mások."""
        derived = tuple((CHILD_DIMENSION_PREFIX + child_id, data["expression"], data.get("method", "check"))
                        for child_id, data in sorted(self.store.get("children", {}).items())
                        if data.get("state") == "active" and data.get("as_dimension"))
        view = KnowledgeView.of(self.active_facts(now))
        from model.working_time.schedule import schedule_from_fact
        schedules = GroupSchedules()
        for fact in sorted(view.of_predicate("munkaido"), key=lambda f: f.created_at):   # a legújabb érvényes
            schedule = schedule_from_fact(fact.args, "tanult" if "tanult" in (fact.note or "") else "kézi")
            if schedule is not None:
                schedules[fact.args["csoport"]] = schedule
        from model.diagnostics.core.threshold_override import ThresholdOverride
        overrides = tuple(ThresholdOverride.from_dict(d) for d in self.store.get("threshold_overrides", {}).values()
                          if d.get("state") == "active")
        return replace(view, derived=derived, schedules=schedules, threshold_overrides=overrides)

    # --- szűkebb hatókörű küszöbök a gyári detektorokhoz ---

    def threshold_overrides(self) -> List[Any]:
        """Az összes szűkebb küszöb (aktív és visszavont).

        Hívja: DashboardService._thresholds_with_custom, DiscoveryService._threshold_suggestions,
        DiscoveryService.knowledge_overview."""
        from model.diagnostics.core.threshold_override import ThresholdOverride
        return [ThresholdOverride.from_dict(d) for d in self.store.get("threshold_overrides", {}).values()]

    def add_threshold_override(self, diagnostic: str, scope: List[List[str]], params: Mapping[str, Any], source: str = "kézi",
                               note: str = "", now: str = "") -> Any:
        """Szűkebb küszöb felvétele (ugyanarra a hatókörre és forrásra a régit visszavonja).

        Hívja: DiscoveryService.close_issue_with_lesson, DiscoveryService.threshold_override_action."""
        from model.diagnostics.core.threshold_override import ThresholdOverride
        if not params:
            raise ValueError("Adj meg legalább egy küszöböt.")
        store = self.store.setdefault("threshold_overrides", {})
        key = (diagnostic, tuple(sorted(tuple(i) for i in scope)), source)
        for data in store.values():   # ugyanarra a hatókörre: a régi helyett az új
            if data.get("state") == "active" and (data["diagnostic"], tuple(sorted(tuple(i) for i in data["scope"])), data["source"]) == key:
                data["state"] = "revoked"
        counters = self.store.setdefault("meta", {}).setdefault("counters", {})
        counters["KUSZ"] = counters.get("KUSZ", 0) + 1
        override = ThresholdOverride(f"KUSZ-{counters['KUSZ']:04d}", diagnostic, [list(i) for i in scope], dict(params),
                                     source, note, now)
        store[override.override_id] = override.to_dict()
        self.save()
        return override

    def revoke_threshold_override(self, override_id: str) -> None:
        """Szűkebb küszöb visszavonása.

        Hívja: DiscoveryService.threshold_override_action."""
        store = self.store.get("threshold_overrides", {})
        if override_id not in store:
            raise ValueError(f"Nincs ilyen küszöb: {override_id}")
        store[override_id]["state"] = "revoked"
        self.save()

    # --- írás ---

    def _next_id(self) -> str:
        """Új tény-azonosító.

        Hívja: KnowledgeBase.add_fact."""
        counters = self.store["meta"]["counters"]
        counters["FACT"] = counters.get("FACT", 0) + 1
        return f"FACT-{counters['FACT']:04d}"

    def add_fact(self, predicate: str, args: Mapping[str, str], now: str = "", valid_until: str = "",
                 note: str = "") -> Tuple[Fact, bool]:
        """Tény felvétele; (tény, új-e). Azonos aktív tény már létezésekor azt adja vissza.

        Ugyanarra a funkcióra és csoportra az elérhetőség és a nem elérhetőség kizárja egymást:
        az új válasz visszavonja a régit.
        

        Hívja: DiscoveryManager.add_fact, learning_sim._knowledge."""
        if predicate not in PREDICATES:
            raise KeyError(f"Ismeretlen predikátum: {predicate}")
        missing = [arg for arg, _ in PREDICATES[predicate].args if not args.get(arg)]
        if missing:
            raise ValueError(f"Hiányzó argumentum: {', '.join(missing)}")
        args = dict(args)
        for fact in self.active_facts(now):
            if fact.predicate == predicate and fact.args == args:
                return fact, False
        exclusive = {ELERHETO.name: NEM_ELERHETO.name, NEM_ELERHETO.name: ELERHETO.name}.get(predicate)
        for fact in self.active_facts(now):
            same_level_group = predicate == SZINT.name and fact.predicate == SZINT.name and fact.args["csoport"] == args["csoport"]
            if (fact.predicate == exclusive and fact.args == args) or same_level_group:
                self.revoke_fact(fact.fact_id)
        fact = Fact(self._next_id(), predicate, args, now, valid_until, note=note)
        self.store["facts"][fact.fact_id] = fact.to_dict()
        self.save()
        return fact, True

    def confirm_fact(self, fact_id: str, now: str) -> Fact:
        """Az elemző megerősíti, hogy a tény továbbra is igaz (a felülvizsgálat ettől számol).

        Hívja: DiscoveryCli.cmd_fact, DiscoveryService.fact."""
        fact = self.fact(fact_id)
        fact.reviewed_at = now
        self.store["facts"][fact_id] = fact.to_dict()
        self.save()
        return fact

    def revoke_fact(self, fact_id: str) -> Fact:
        """Tény visszavonása.

        Hívja: DiscoveryManager.revoke_fact, KnowledgeBase.add_fact."""
        fact = self.fact(fact_id)
        fact.state = RuleState.REVOKED.value
        self.store["facts"][fact_id] = fact.to_dict()
        self.save()
        return fact

    def import_facts(self, facts: Mapping[str, Mapping[str, Any]]) -> int:
        """Korábbi tények átvétele (pl. a feltárási állapotból); az azonosítók megmaradnak.

        Hívja: DiscoveryCli.__init__, DiscoveryService.__init__."""
        imported = 0
        for fact_id, data in facts.items():
            if fact_id not in self.store["facts"]:
                self.store["facts"][fact_id] = dict(data)
                imported += 1
                number = int(fact_id.rsplit("-", 1)[-1]) if fact_id.rsplit("-", 1)[-1].isdigit() else 0
                counters = self.store["meta"]["counters"]
                counters["FACT"] = max(counters.get("FACT", 0), number)
        if imported:
            self.save()
        return imported
