from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Mapping, Optional, Tuple

from model.knowledge.predicates import L1_LEVEL
from model.timeline.field_change import FieldChange
from model.timeline.fields import CLOSED_STATES
from model.timeline.interval import Interval
from model.timeline.stint import Stint


@dataclass(frozen=True)
class TicketTimeline:
    incident_id: str
    as_of: datetime
    changes: Tuple[FieldChange, ...]    # időrendben (azonos időpontnál a history sorrendjében)
    stints: Tuple[Stint, ...]
    holds: Tuple[Interval, ...]

    @property
    def opened_at(self) -> datetime:
        return self.changes[0].at

    @property
    def state(self) -> str:
        return self.value_at("state")

    @property
    def is_closed(self) -> bool:
        """Az `as_of` pillanatában lezárt (megoldott vagy zárt) állapotban van."""
        return self.state in CLOSED_STATES

    @property
    def first_closed_at(self) -> Optional[datetime]:
        """Az első lezárás ideje (újranyitás után is megmarad)."""
        return next((c.at for c in self.changes if c.field == "state" and c.new in CLOSED_STATES), None)

    @property
    def groups(self) -> List[str]:
        """Az érintett csoportok az első érintés sorrendjében (ismétlés nélkül)."""
        seen: List[str] = []
        for stint in self.stints:
            if stint.group not in seen:
                seen.append(stint.group)
        return seen

    def changes_of(self, field: str) -> List[FieldChange]:
        return [c for c in self.changes if c.field == field]

    def reassignments(self, levels: Mapping[str, str] = {}) -> List[FieldChange]:
        """Csoportváltások egyik kitöltött csoportról egy másikra. Az L1-ből való továbbítás a normál
        út, nem számít (ha a szintek ismertek); az L1-be való visszakerülés igen."""
        return [
            c for c in self.changes_of("assignment_group")
            if c.old and c.new and c.old != c.new
            and not (levels.get(c.old) == L1_LEVEL and levels.get(c.new) != L1_LEVEL)
        ]

    def first_value(self, field: str) -> str:
        """Az első kitöltött érték (pl. a nyitáskori csoport)."""
        return next((c.new for c in self.changes if c.field == field and c.new), "")

    def value_at(self, field: str, moment: Optional[datetime] = None) -> str:
        """A mező értéke egy pillanatban (az addig, azzal együtt történt változások után)."""
        moment = moment or self.as_of
        value = ""
        for change in self.changes:
            if change.at > moment:
                break
            if change.field == field:
                value = change.new
        return value

    def values_at(self, moment: Optional[datetime] = None, before: bool = False) -> Dict[str, str]:
        """Minden mező értéke egy pillanatban; `before`: a pillanatban történt változások nélkül
        (pl. egy szakasz végén az átadás előtti állapot)."""
        moment = moment or self.as_of
        values: Dict[str, str] = {}
        for change in self.changes:
            if change.at > moment or (before and change.at == moment):
                break
            values[change.field] = change.new
        return values

    def next_stint(self, stint: Stint) -> Optional[Stint]:
        return self.stints[stint.index + 1] if stint.index + 1 < len(self.stints) else None

    def handover_changes(self, stint: Stint) -> List[FieldChange]:
        """A továbbítással egy mentésben történt változások (a csoportváltás pillanatában), a
        csoportváltás nélkül. Ezeket a küldő csoport végezte, bár időben a következő szakaszba esnek."""
        following = self.next_stint(stint)
        if following is None:
            return []
        return [c for c in following.changes if c.at == following.start and c.field != "assignment_group"]

    def own_changes(self, stint: Stint) -> List[FieldChange]:
        """A csoport saját változásai a szakaszban: az érkezés pillanatának (a küldő vagy a ticket
        létrehozása által végzett) változásai nélkül, a saját továbbítási mentésével együtt."""
        return [c for c in stint.changes if c.at > stint.start] + self.handover_changes(stint)

    def stint_at(self, moment: datetime) -> Optional[Stint]:
        return next((s for s in self.stints if s.start <= moment < s.end or (s.is_open and moment >= s.start)), None)
