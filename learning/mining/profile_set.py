from dataclasses import dataclass
from typing import Tuple

from learning.mining.measure_def import MeasureDef
from learning.mining.ticket_profile import TicketProfile


@dataclass(frozen=True)
class ProfileSet:
    """Egy ablak adatlapjai mérési egységenként (ticket / szakasz)."""

    tickets: Tuple[TicketProfile, ...]
    stints: Tuple[TicketProfile, ...]

    def for_measure(self, measure: MeasureDef) -> Tuple[TicketProfile, ...]:
        """A mérés egységének adatlapjai (ticket vagy szakasz).

        Hívja: DiscoveryService._unit_sets, candidate_analysis.analyze_candidate, candidate_analysis.breakdown,
        pattern_finding_diagnosis.measure_pattern, runner._counts, runner._mine_all, runner._mine_trends."""
        return self.stints if measure.scope == "stint" else self.tickets
