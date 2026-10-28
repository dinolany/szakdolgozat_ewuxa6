from dataclasses import dataclass, field
from typing import Any, Dict, List

from learning.discovery_manager import DiscoveryManager


@dataclass
class Track:
    """Egy kör (tanuló vagy összehasonlító) állapota és napi mérései."""
    name: str
    learning: bool
    manager: DiscoveryManager
    daily: List[Dict[str, Any]] = field(default_factory=list)
    tips: List[bool] = field(default_factory=list)
    generalizations: List[Dict[str, Any]] = field(default_factory=list)
    fault_log: Dict[str, Dict[str, str]] = field(default_factory=dict)   # hiba -> első jelölt / elé került / finding
    open_issues: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # issue -> {candidate_id, opened}
    noise_elements: Dict[tuple, int] = field(default_factory=dict)        # elvetett zajok elemei -> hányszor
    suppressed_elements: List[str] = field(default_factory=list)
