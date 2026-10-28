"""Mintafeltárás: ticket-adatlapok, mérési katalógus és kontrasztminta-keresés."""

from learning.mining.measures import MEASURES, MEASURES_BY_KEY
from learning.mining.measure_def import MeasureDef
from learning.mining.mining_params import MiningParams
from learning.mining.pattern_result import PatternResult
from learning.mining.contrast_mining import mine
from learning.mining.pattern import pattern_key
from learning.mining.profiles import DIMENSIONS, TIME_DIMENSIONS, build_profile
from learning.mining.ticket_profile import TicketProfile

__all__ = [
    "MEASURES",
    "MEASURES_BY_KEY",
    "MeasureDef",
    "MiningParams",
    "PatternResult",
    "mine",
    "pattern_key",
    "DIMENSIONS",
    "TIME_DIMENSIONS",
    "TicketProfile",
    "build_profile",
]
