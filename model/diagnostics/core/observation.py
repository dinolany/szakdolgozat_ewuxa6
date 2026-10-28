from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Observation:
    """Amit a konkrét diagnosztika egy subjectről megállapít, küszöbtől függetlenül."""

    subject: str
    details: Mapping[str, Any]
    affected: Tuple[str, ...] = ()
    history_refs: Tuple[Dict[str, Any], ...] = ()
    trigger_ref: Optional[Dict[str, Any]] = None  # csak incidensalapú mérésnél: a küszöbátlépés eseménye
