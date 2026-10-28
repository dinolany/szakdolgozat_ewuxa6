"""A feltárási ciklus adatmodelljei: jelölt, döntés, tény, szabály és a predikátumkatalógus.

Minden objektum JSON-ba menthető (to_dict / from_dict). A szabályok Datalog-szerűek:
nem rekurzív "feltétel -> teendő" formájúak, opcionálisan tényekre hivatkozva.
"""

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from model.diagnostics.core.suppression import conditions_match, values_of
from learning.measures import MEASURES_BY_KEY
from learning.mining import pattern_key, with_implied
from model.knowledge.knowledge_base import (  # noqa: F401  (a tények a közös tudástárban élnek)
    ELERHETO,
    NEM_DOLGOZIK,
    NEM_ELERHETO,
    PREDICATES,
    SZINT,
    Fact,
    PredicateDef,
    RuleState,
)


class CandidateState(str, Enum):
    QUEUED = "queued"                # döntésre vár
    FILTERED = "filtered"            # egy szabály elrejtette
    ACCEPTED = "accepted"            # finding: issue lesz belőle
    REJECTED_ONCE = "rejected_once"  # elvetve erre az előfordulásra; új előfordulásnál visszajön
    REJECTED = "rejected"            # elvetve (szabály rögzíti)
    BASELINED = "baselined"          # ismert eltérés: csak a saját szokásos eltéréséhez képesti romlás jelez
    ATTACHED = "attached"            # egy nyitott issue (feladat) mintájához illik: az issue-hoz csatolva, nem kerül a sorba
    REFINED = "refined"              # tovább bontva: maga nem jelez, csak a benne (hozzá képest) kiugró szűkítései


class Verdict(str, Enum):
    FINDING = "finding"
    NOT_FINDING = "not_finding"
    KNOWN_DEVIATION = "known_deviation"
    REFINE = "refine"                # a minta önmagában nem finding, de ezen belül keressen tovább


class Scope(str, Enum):
    THIS_OCCURRENCE = "this_occurrence"
    THIS_PATTERN = "this_pattern"
    RULE = "rule"


class RuleKind(str, Enum):
    REJECTION = "rejection"  # elvetett minta
    KNOWLEDGE = "knowledge"  # tényre épülő szakmai tudás


class RuleAction(str, Enum):
    DROP = "drop"
    BOOST = "boost"


def make_candidate_id(measure: str, key: str) -> str:
    """A jelölt stabil azonosítója a mérésből és a minta kulcsából.

    Hívja: DiscoveryManager.ingest, DiscoveryManager.ingest_signals, runner.run_discovery."""
    return "CAND-" + hashlib.sha1(f"{measure}|{key}".encode("utf-8")).hexdigest()[:12]


def _parse_key(key: str) -> Tuple[Tuple[str, str], ...]:
    """A minta kulcsából (mező=érték&…) a mintaelemek.

    Hívja: Candidate.descriptions, Candidate.items."""
    return tuple(tuple(part.split("=", 1)) for part in key.split("&") if "=" in part)


# --------------------------------------------------------------------------
# Objektumok
# --------------------------------------------------------------------------

@dataclass
class Candidate:
    candidate_id: str
    measure: str
    key: str                                  # a képviselő minta kulcsa
    alternatives: List[str] = field(default_factory=list)
    state: str = CandidateState.QUEUED.value
    stats: Dict[str, Any] = field(default_factory=dict)
    bad_incident_ids: List[str] = field(default_factory=list)
    window_start: str = ""
    window_end: str = ""
    first_seen_at: str = ""
    last_seen_at: str = ""
    occurrences: List[Dict[str, Any]] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)       # pl. returned, exceeds_baseline, rule_revoked, child_alert
    filtered_by: Optional[Dict[str, Any]] = None          # {rule_id, version, fact_id}
    boosted_by: Optional[Dict[str, Any]] = None
    rejected_window_end: str = ""
    suggestion: Optional[Dict[str, Any]] = None           # {verdict, confidence, reasons}
    priority: float = 0.0
    issue_id: Optional[str] = None                        # a findingból lett issue
    attached_to: Optional[str] = None                     # a nyitott issue, amelyhez csatolva van
    not_now: int = 0                                      # hányszor mondta rá az elemző: most nem (visszatérve egyre előrébb)
    feedback: Dict[str, Any] = field(default_factory=dict)  # a bontás értékelései: methods {almérés: verdict}, breakdown_done
    covered_by: Optional[str] = None                      # egy elfogadott finding kulcsa, amely lefedi
    reach: List[str] = field(default_factory=list)        # döntésre váró szűkítései: a döntése ezeket is érinti
    # contrast: a többihez képest; trend: a saját múltjához képest; signal: egy jóváhagyásos diagnosztika
    # jelzése (a measure ilyenkor a diagnosztika kulcsa, a stats a megfigyelés részletei)
    kind: str = "contrast"
    # Futásonkénti darabszámok akkor is, ha a minta nem volt kiugró (a szokásos eltérés forrása):
    # [{measured_at, window_start, window_end, bad, size, population_bad, population}]
    counts: List[Dict[str, Any]] = field(default_factory=list)
    baseline: Optional[Dict[str, Any]] = None             # befagyasztott szokásos eltérés (ismert eltérésnél)

    @property
    def items(self) -> Tuple[Tuple[str, str], ...]:
        """A jelölt képviselő mintájának elemei.

        Hívja: BacklogAgingDiagnosis._observe, BaseDiagnosis._to_measurement, BaseDiagnosis.entity_items,
        Candidate.descriptions (átadva), Candidate.from_dict, DashboardService.incident_detail,
        DashboardService.incidents, DashboardService.issues, Decision.from_dict és mások."""
        return _parse_key(self.key)

    def descriptions(self) -> List[Tuple[Tuple[str, str], ...]]:
        """A képviselő és az alternatív leírások.

        Hívja: DiscoveryManager._covering_finding, DiscoveryManager._entries, DiscoveryManager._features,
        DiscoveryManager.contexts, DiscoveryService._refinements, DiscoveryService._run_probes,
        DiscoveryService.knowledge_overview, backlog.candidate_groups, children.trigger_matches és mások."""
        return [self.items] + [_parse_key(key) for key in self.alternatives]

    def to_dict(self) -> Dict[str, Any]:
        """A jelölt tárolható szótárként.

        Hívja: DiscoveryManager._save_candidate, DiscoveryService._check_known_faults,
        DiscoveryService.children_overview."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Candidate":
        """A tárolt szótárból; a megszűnt mezőket (pl. a régi figyelésé) átugorja.

        Hívja: DiscoveryManager.candidate, DiscoveryManager.candidates, DiscoveryManager.ingest,
        DiscoveryManager.ingest_signals."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Decision:
    decision_id: str
    candidate_id: str
    at: str
    verdict: str
    scope: Optional[str] = None
    rule_id: Optional[str] = None
    fact_id: Optional[str] = None
    note: str = ""
    suggestion_snapshot: Optional[Dict[str, Any]] = None
    stats_snapshot: Dict[str, Any] = field(default_factory=dict)
    auto: str = ""           # almérés döntötte el (azonosítója): nem fogyaszt keretet, nem tanítja a tippet

    def to_dict(self) -> Dict[str, Any]:
        """A döntés tárolható szótárként.

        Hívja: DiscoveryManager.decide, DiscoveryService.config_schema, DiscoveryService.overview."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Decision":
        """A tárolt szótárból; a megszűnt mezőket (figyelés, csomópont) átugorja.

        Hívja: DiscoveryManager.decisions."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class Rule:
    rule_id: str
    kind: str
    action: str
    conditions: List[Dict[str, Any]] = field(default_factory=list)  # [{dim, values: [...]}]
    fact_binding: Optional[Dict[str, Any]] = None                    # {predicate, bind: {arg: dim}}
    measure_filter: Dict[str, Any] = field(default_factory=lambda: {"mode": "any"})  # any / list / time_sensitive
    state: str = RuleState.ACTIVE.value
    version: int = 1
    created_at: str = ""
    valid_until: str = ""
    source: str = "manual"
    reason: str = ""
    hits: int = 0
    last_hit_at: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)      # korábbi verziók
    reviewed_at: str = ""                                            # az elemző utolsó megerősítése
    hard: bool = False                                               # kifejezett elnyomás: az áttörő erősségű jelöltet is rejti

    def is_active(self, now: str) -> bool:
        """A szabály érvényes-e a megadott pillanatban (aktív, és nem járt le).

        Hívja: DiscoveryManager.apply_rules, DiscoveryService.detector_rules, knowledge.match_rule."""
        return self.state == RuleState.ACTIVE.value and (not self.valid_until or now <= self.valid_until)

    def measure_applies(self, measure_key: str) -> bool:
        """A szabály vonatkozik-e erre a mérésre (vagy detektorra).

        Hívja: knowledge.match_rule."""
        mode = self.measure_filter.get("mode", "any")
        if mode == "list":
            return measure_key in self.measure_filter.get("measures", [])
        if mode == "time_sensitive":
            measure = MEASURES_BY_KEY.get(measure_key)
            return bool(measure and measure.time_sensitive)
        return True

    def render(self) -> str:
        """Prolog-szerű megjelenítés.

        Hívja: DiscoveryCli.cmd_rule, DiscoveryCli.cmd_rules, DiscoveryService._effect, DiscoveryService.rule."""
        head = "elvet(J)" if self.action == RuleAction.DROP.value else "kiemel(J)"
        body = []
        for condition in self.conditions:
            values = condition["values"]
            body.append(f"J.{condition['dim']} = (bármely érték)" if values == ["*"] else
                        f"J.{condition['dim']} = {values[0]}" if len(values) == 1
                        else f"J.{condition['dim']} ∈ {{{', '.join(values)}}}")
        if self.fact_binding:
            predicate = PREDICATES[self.fact_binding["predicate"]]
            bind = self.fact_binding["bind"]
            variables = {arg: arg[0].upper() for arg, _ in predicate.args}
            body.extend(f"J.{bind[arg]} = {variables[arg]}" for arg, _ in predicate.args)
            body.append(f"{predicate.name}({', '.join(variables[arg] for arg, _ in predicate.args)})")
        mode = self.measure_filter.get("mode", "any")
        if mode == "time_sensitive":
            body.append("időfüggő(J.mérés)")
        elif mode == "list":
            body.append(f"J.mérés ∈ {{{', '.join(self.measure_filter.get('measures', []))}}}")
        return f"{head} :- {', '.join(body) or 'true'}."

    def to_dict(self) -> Dict[str, Any]:
        """A szabály tárolható szótárként.

        Hívja: DiscoveryManager._save_rule, DiscoveryManager.revoke_rule, DiscoveryService.rule."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Rule":
        """A szabály a tárolt szótárból.

        Hívja: DiscoveryManager.apply_rules, DiscoveryManager.confirm_rule, DiscoveryManager.revoke_rule,
        DiscoveryManager.rules, DiscoveryService._effect, DiscoveryService._suppress, DiscoveryService.detector_rules,
        learning_sim._decide."""
        return cls(**dict(data))


# --------------------------------------------------------------------------
# Illesztés
# --------------------------------------------------------------------------

def match_rule(rule: Rule, candidate: Candidate, facts: Sequence[Fact], now: str) -> Optional[Dict[str, Any]]:
    """Illeszkedik-e a szabály a jelöltre; ha igen, a találat leírása (melyik leírás, melyik tény).

    Egy leírás akkor illik, ha tartalmazza a szabály összes feltételét (a dátumból következő
    hét napját is figyelembe véve). Összevont jelöltnél bármelyik leírás illeszkedése elég.
    

    Hívja: DiscoveryManager.apply_rules."""
    if not rule.is_active(now) or not rule.measure_applies(candidate.measure):
        return None
    for description in candidate.descriptions():
        # egy mezőnek több értéke is lehet (pl. a jegy útjába eső csoportok): bármelyik illeszkedése elég
        values = values_of(with_implied(tuple(sorted(description))))
        if not conditions_match(rule.conditions, values):
            continue
        if not rule.fact_binding:
            return {"rule_id": rule.rule_id, "version": rule.version, "fact_id": None, "matched": pattern_key(description)}
        predicate = rule.fact_binding["predicate"]
        bind = rule.fact_binding["bind"]
        for fact in facts:
            if fact.predicate != predicate or not fact.is_active(now):
                continue
            if all(fact.args.get(arg) in values.get(dim, set()) for arg, dim in bind.items()):
                return {"rule_id": rule.rule_id, "version": rule.version, "fact_id": fact.fact_id,
                        "matched": pattern_key(description)}
    return None
