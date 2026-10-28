from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping

from model.knowledge.predicates import PREDICATES
from model.knowledge.rule_state import RuleState


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
