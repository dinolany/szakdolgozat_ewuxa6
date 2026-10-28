from dataclasses import dataclass
from datetime import datetime
from typing import Tuple

from model.timeline.field_change import FieldChange


@dataclass(frozen=True)
class Occasion:
    """Egy alkalom, amikor a funkció alkalmazható volt, és hogy élt-e vele a csoport."""

    feature: str
    group: str
    incident_id: str
    at: datetime
    used: bool
    anchor: FieldChange                   # az alkalmat kiváltó esemény (pl. hold kezdete, továbbítás)
    evidence: Tuple[FieldChange, ...]     # a használat eseményei
    stint: int = -1                       # a szakasz sorszáma a ticketben (almérésekhez)
