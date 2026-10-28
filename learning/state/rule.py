from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence

from learning.mining.contrast_mining import pattern_key, with_implied
from learning.mining.measures import MEASURES_BY_KEY
from learning.state.candidate import Candidate
from model.diagnostics.core.suppression import conditions_match, values_of
from model.knowledge.fact import Fact
from model.knowledge.predicates import PREDICATES
from model.knowledge.rule_state import RuleState


class RuleAction(str, Enum):
    DROP = "drop"
    BOOST = "boost"


class RuleKind(str, Enum):
    REJECTION = "rejection"  # elvetett minta
    KNOWLEDGE = "knowledge"  # tényre épülő szakmai tudás


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

        Hívja: DiscoveryManager.apply_rules, DiscoveryService.detector_rules, rule.match_rule."""
        return self.state == RuleState.ACTIVE.value and (not self.valid_until or now <= self.valid_until)

    def measure_applies(self, measure_key: str) -> bool:
        """A szabály vonatkozik-e erre a mérésre (vagy detektorra).

        Hívja: rule.match_rule."""
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
