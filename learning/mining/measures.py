from typing import Dict

from learning.mining.measure_def import MeasureDef


PICKUP_DELAY = MeasureDef(
    "pickup_delay", "Lassú felvétel", "duration", "óra", 8, True,
    "Nyitástól az első felelőshöz rendelésig eltelt idő; ha még senki nem vette fel, a várakozás eddigi ideje.",
    reason="{value} óra telt el a nyitástól az első felelősig",
    pending_reason="még nincs felelőse: {value} órája vár",
)

HOLD_TIME = MeasureDef(
    "hold_time", "Hosszú hold-idő", "duration", "óra", 24, True,
    "A hold (felfüggesztett) állapotban töltött idő összesen.",
    reason="összesen {value} órát volt hold állapotban",
)

REASSIGNMENTS = MeasureDef(
    "reassignments", "Ping-pong", "count", "db", 3, False,
    "Hányszor került a ticket másik megoldócsoporthoz (az L1-ből való továbbítás nem számít, ha az L1 ismert).",
    reason="{value} csoportváltás",
)

SLA_ESCALATION = MeasureDef(
    "sla_escalation", "SLA-eszkaláció", "flag", "", 1, False,
    "A ticket SLA-eszkalációt kapott.",
    reason="SLA-eszkalációt kapott",
)

STINT_PICKUP = MeasureDef(
    "stint_pickup", "Lassú felvétel a csoportnál", "duration", "óra", 8, True,
    "A csoporthoz érkezéstől az első ottani felelőshöz rendelésig eltelt idő; ha még senki nem vette fel, "
    "a várakozás eddigi ideje (felelős nélkül továbbadott vagy lezárt szakaszra nem értelmezett).",
    scope="stint", reason="a csoporthoz érkezéstől {value} óra telt el az ottani első felelősig",
    pending_reason="a csoportnál még nincs felelőse: {value} órája vár",
)

STINT_HOLD = MeasureDef(
    "stint_hold", "Hosszú hold a csoportnál", "duration", "óra", 24, True,
    "A csoportnál töltött szakasz alatti hold (felfüggesztett) idő.",
    scope="stint", reason="a csoportnál {value} órát volt hold állapotban",
)

MEASURES = (PICKUP_DELAY, HOLD_TIME, REASSIGNMENTS, SLA_ESCALATION, STINT_PICKUP, STINT_HOLD)

MEASURES_BY_KEY: Dict[str, MeasureDef] = {measure.key: measure for measure in MEASURES}
