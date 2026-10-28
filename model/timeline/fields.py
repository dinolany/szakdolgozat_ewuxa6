from datetime import datetime
from typing import Any


FIELD_ALIASES = {
    "u_assignment_group": "assignment_group",
    "u_business_service": "business_service",
    "service": "business_service",
    "incident_state": "state",
}

EMPTY_VALUES = {"", "n/a", "none", "null"}

HOLD_STATE = "3"

CLOSED_STATES = frozenset({"6", "7"})

def canonical_field(raw: Any) -> str:
    """A history mezőneve egységesítve (pl. u_assignment_group -> assignment_group)."""
    name = str(raw or "").strip().lower()
    return FIELD_ALIASES.get(name, name)

def clean_value(raw: Any) -> str:
    text = str(raw).strip() if raw is not None else ""
    return "" if text.lower() in EMPTY_VALUES else text

def hours_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600)
