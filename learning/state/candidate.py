"""A feltárási ciklus adatmodelljei: jelölt, döntés, tény, szabály és a predikátumkatalógus.

Minden objektum JSON-ba menthető (to_dict / from_dict). A szabályok Datalog-szerűek:
nem rekurzív "feltétel -> teendő" formájúak, opcionálisan tényekre hivatkozva.
"""

from dataclasses import asdict, dataclass, field
import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple

from learning.state.candidate_state import CandidateState


def make_candidate_id(measure: str, key: str) -> str:
    """A jelölt stabil azonosítója a mérésből és a minta kulcsából.

    Hívja: DiscoveryManager.ingest, DiscoveryManager.ingest_signals, runner.run_discovery."""
    return "CAND-" + hashlib.sha1(f"{measure}|{key}".encode("utf-8")).hexdigest()[:12]

def _parse_key(key: str) -> Tuple[Tuple[str, str], ...]:
    """A minta kulcsából (mező=érték&…) a mintaelemek.

    Hívja: Candidate.descriptions, Candidate.items."""
    return tuple(tuple(part.split("=", 1)) for part in key.split("&") if "=" in part)

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
        DiscoveryService.knowledge_overview, backlog.candidate_groups, child_def.trigger_matches és mások."""
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
