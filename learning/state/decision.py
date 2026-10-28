from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional


class Scope(str, Enum):
    THIS_OCCURRENCE = "this_occurrence"
    THIS_PATTERN = "this_pattern"
    RULE = "rule"


class Verdict(str, Enum):
    FINDING = "finding"
    NOT_FINDING = "not_finding"
    KNOWN_DEVIATION = "known_deviation"
    REFINE = "refine"                # a minta önmagában nem finding, de ezen belül keressen tovább


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
