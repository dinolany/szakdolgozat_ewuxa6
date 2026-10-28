"""Közös tudástár: az elemző által rögzített tények a világról, amelyeket a feltárás és minden
diagnosztika ugyanúgy lát (pl. melyik csoport L1, melyik csoport mikor nem dolgozik, melyik
ServiceNow-funkció elérhető kinek).

A tár saját JSON-fájlban él, függetlenül a feltárási állapottól és az incidens-adatbázistól.
A diagnosztikák egy adott pillanatra érvényes, csak olvasható nézetet kapnak (KnowledgeView)
a mérési kontextusban; a tényeket a feltárási konzol (vagy más felület) írja.
"""

import json
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


class RuleState(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


# --------------------------------------------------------------------------
# Predikátumok
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PredicateDef:
    """Egy tényfajta: argumentumai és az, hogy melyik argumentum melyik dimenzióhoz kötődik."""

    name: str
    args: Tuple[Tuple[str, str], ...]  # (argumentum, dimenzió)
    description: str
    # drop: a tényhez tudásszabály tartozik, amely elveti az illeszkedő jelölteket;
    # classify: a tény csak osztályoz (pl. melyik csoport L1), szabály nem tartozik hozzá
    kind: str = "drop"

    def render(self, args: Mapping[str, Any]) -> str:
        """A tény szövege a predikátum alakjában (pl. nem_dolgozik(G, Sat))."""
        return f"{self.name}({', '.join(str(args.get(arg, '?')) for arg, _ in self.args)})"


NEM_DOLGOZIK = PredicateDef(
    "nem_dolgozik",
    (("csoport", "assignment_group"), ("nap", "weekday")),
    "A megoldócsoport az adott napon nem dolgozik (nincs rendelkezésre állás).",
)
SZINT = PredicateDef(
    "szint",
    (("csoport", "assignment_group"), ("szint", "level")),
    "A megoldócsoport támogatási szintje (pl. L1: központi első szint).",
    kind="classify",
)
ELERHETO = PredicateDef(
    "elerheto",
    (("funkcio", "feature"), ("csoport", "assignment_group")),
    "A ServiceNow funkció elérhető a csoportnak (rajta van az űrlapjukon); csoport=* : mindenkinek.",
    kind="classify",
)
NEM_ELERHETO = PredicateDef(
    "nem_elerheto",
    (("funkcio", "feature"), ("csoport", "assignment_group")),
    "A ServiceNow funkció nem elérhető a csoportnak (nincs az űrlapjukon); csoport=* : senkinek.",
    kind="classify",
)
NEM_ELVART = PredicateDef(
    "nem_elvart",
    (("funkcio", "feature"), ("csoport", "assignment_group")),
    "A funkció használata ennél a csoportnál nem elvárt (pl. azonnal zárnak close notes-szal): a hiánya nem edukációs jelzés.",
)
MUNKAIDO = PredicateDef(
    "munkaido",
    (("csoport", "assignment_group"), ("napok", "weekdays"), ("tol", "hour"), ("ig", "hour")),
    "A csoport munkaideje: napok (Mon,Tue,… vagy *), tól–ig egész órában (ig: 1–24). A felvételi és a hold idő "
    "ennél a csoportnál munkaórában számít.",
    kind="classify",
)
PREDICATES: Dict[str, PredicateDef] = {p.name: p for p in (NEM_DOLGOZIK, SZINT, ELERHETO, NEM_ELERHETO, NEM_ELVART, MUNKAIDO)}
L1_LEVEL = "L1"
ALL_GROUPS = "*"


@dataclass
class Fact:
    fact_id: str
    predicate: str
    args: Dict[str, str]
    created_at: str = ""
    valid_until: str = ""
    state: str = RuleState.ACTIVE.value
    note: str = ""
    reviewed_at: str = ""   # az elemző utolsó megerősítése (felülvizsgálathoz)

    def render(self) -> str:
        """A tény szövege a Tudástárhoz.

        Hívja: DiscoveryCli.cmd_available, DiscoveryCli.cmd_fact, DiscoveryCli.cmd_facts,
        DiscoveryService.availability, DiscoveryService.fact."""
        return PREDICATES[self.predicate].render(self.args) + "."

    def is_active(self, now: str) -> bool:
        """A tény érvényes-e a megadott pillanatban.

        Hívja: KnowledgeBase.active_facts, knowledge.match_rule."""
        return self.state == RuleState.ACTIVE.value and (not self.valid_until or not now or now <= self.valid_until)

    def to_dict(self) -> Dict[str, Any]:
        """A tény tárolható szótárként.

        Hívja: DiscoveryService.availability, DiscoveryService.fact, KnowledgeBase.add_fact,
        KnowledgeBase.confirm_fact, KnowledgeBase.revoke_fact."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Fact":
        """A tény a tárolt szótárból.

        Hívja: KnowledgeBase.fact, KnowledgeBase.facts."""
        return cls(**dict(data))


# --------------------------------------------------------------------------
# Nézet (a diagnosztikáknak)
# --------------------------------------------------------------------------

class GroupSchedules(dict):
    """Csoport -> munkaidő. A "*" kulcs a cégszintű munkaidő: annál a csoportnál számít, amelyiknek nincs saját
    (tanult vagy kézi) munkaideje. Az `in` csak a csoport saját munkaidejét nézi (a javaslatok ehhez mérnek)."""

    def get(self, key, default=None):
        """A csoport munkaideje: a saját, különben a cégszintű („*”), különben az alapérték.

        Hívja: BacklogAgingDiagnosis._age, DiscoveryService.backlog_calendar, children._schedule_of,
        profile.stint_profiles."""
        value = dict.get(self, key)
        if value is None and key != "*":
            value = dict.get(self, "*")
        return default if value is None else value


@dataclass(frozen=True)
class KnowledgeView:
    """Az egy pillanatban érvényes tények, csak olvasható formában."""

    facts: Tuple[Fact, ...] = ()
    levels: Mapping[str, str] = field(default_factory=dict)                      # csoport -> szint
    availability: Mapping[Tuple[str, str], bool] = field(default_factory=dict)   # (funkció, csoport) -> elérhető
    # keresési szempontként használt almérések: (dimenzió neve, kifejezés); az adatlapokon igen/nem értékkel
    derived: Tuple[Tuple[str, str], ...] = ()
    # csoport -> munkaidő (models.working_time.Schedule): a felvételi és a hold idő munkaórában
    schedules: Mapping[str, Any] = field(default_factory=dict)
    # a gyári detektorok szűkebb hatókörű küszöbei (models.diagnostics.core.threshold_overrides.ThresholdOverride)
    threshold_overrides: Tuple[Any, ...] = ()
    # a tanuló kör erre a detektorra szóló elvető szabályai (models.diagnostics.core.suppression)
    suppressions: Tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def of(cls, facts: List[Fact]) -> "KnowledgeView":
        """A nézet a tényekből: csoportszintek és funkció-elérhetőség.

        Hívja: KnowledgeBase.view."""
        levels = {f.args["csoport"]: f.args["szint"] for f in facts if f.predicate == SZINT.name}
        availability = {(f.args["funkcio"], f.args["csoport"]): f.predicate == ELERHETO.name
                        for f in facts if f.predicate in (ELERHETO.name, NEM_ELERHETO.name)}
        return cls(tuple(facts), levels, availability)

    def of_predicate(self, predicate: str) -> List[Fact]:
        """Az adott predikátum tényei.

        Hívja: KnowledgeBase.view."""
        return [fact for fact in self.facts if fact.predicate == predicate]


EMPTY_VIEW = KnowledgeView()
CHILD_DIMENSION_PREFIX = "almeres:"   # az almérésből képzett dimenzió neve: almeres:<azonosító>


# --------------------------------------------------------------------------
# Tár
# --------------------------------------------------------------------------

class KnowledgeBase:
    """A tények tára. `path` nélkül csak a megadott szótárban (pl. egy másik állapot részeként) él."""

    def __init__(self, store: Optional[Dict[str, Any]] = None, path: Optional[str] = None):
        """A közös tudástár (fájlból, vagy tesztben memóriában).

        Létrehozza: analyzer, discovery_cli, synthetic.learning_sim, models.discovery.manager."""
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
        from models.working_time import schedule_from_fact   # késleltetett: a working_time nem függ a tudástártól
        schedules = GroupSchedules()
        for fact in sorted(view.of_predicate("munkaido"), key=lambda f: f.created_at):   # a legújabb érvényes
            schedule = schedule_from_fact(fact.args, "tanult" if "tanult" in (fact.note or "") else "kézi")
            if schedule is not None:
                schedules[fact.args["csoport"]] = schedule
        from models.diagnostics.core.threshold_overrides import ThresholdOverride   # késleltetett import
        overrides = tuple(ThresholdOverride.from_dict(d) for d in self.store.get("threshold_overrides", {}).values()
                          if d.get("state") == "active")
        return replace(view, derived=derived, schedules=schedules, threshold_overrides=overrides)

    # --- szűkebb hatókörű küszöbök a gyári detektorokhoz ---

    def threshold_overrides(self) -> List[Any]:
        """Az összes szűkebb küszöb (aktív és visszavont).

        Hívja: DashboardService._thresholds_with_custom, DiscoveryService._threshold_suggestions,
        DiscoveryService.knowledge_overview."""
        from models.diagnostics.core.threshold_overrides import ThresholdOverride
        return [ThresholdOverride.from_dict(d) for d in self.store.get("threshold_overrides", {}).values()]

    def add_threshold_override(self, diagnostic: str, scope: List[List[str]], params: Mapping[str, Any], source: str = "kézi",
                               note: str = "", now: str = "") -> Any:
        """Szűkebb küszöb felvétele (ugyanarra a hatókörre és forrásra a régit visszavonja).

        Hívja: DiscoveryService.close_issue_with_lesson, DiscoveryService.threshold_override_action."""
        from models.diagnostics.core.threshold_overrides import ThresholdOverride
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
