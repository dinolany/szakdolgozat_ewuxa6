from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class MeasureDef:
    """Egy ticketen mérhető mennyiség, és mikor "érintett" egy eset (a mintakereső bitfeje).

    Érintett az az eset, amelynek mért értéke kiugró: átlépi a mérés küszöbét. A felületen a minta
    érintett résztvevőiről beszélünk; az `explain` soronként megmondja, miért érintett egy eset.
    Hogy hol csoportosulnak az érintett esetek, azt a mintakereső találja meg.
    """

    key: str
    label: str
    kind: str                 # "duration" (óra) / "count" / "flag" (0 vagy 1)
    unit: str
    bad_threshold: float      # rossz, ha az érték >= küszöb
    time_sensitive: bool      # függ-e a munkaidőtől (a szabályok hivatkozhatnak rá)
    description: str
    # ticket: a mérés egysége a ticket (nyitáskori időpont);
    # stint: egy csoportnál töltött szakasz (az érkezés időpontja, a csoport a szakasz csoportja);
    # az L1-szakaszok nem tartoznak bele, ha a szint-tények megnevezik az L1-et
    scope: str = "ticket"
    # Soronkénti ok: {value} a mért érték; a még folyamatban lévő esetre (pl. még nincs felelőse) külön szöveg
    reason: str = "{value}"
    pending_reason: str = ""

    def is_bad(self, value: Optional[float], threshold: Optional[float] = None) -> bool:
        """Az érték érintett (kiugró) eset-e a küszöb szerint.

        Hívja: DiscoveryService._unit_sets, MeasureDef.explain, analysis.analyze_candidate, analysis.breakdown,
        baseline.pattern_counts, compare.window_summary, mining.mine, pattern_finding.measure_pattern, trend._counts."""
        if value is None:
            return False
        return value >= (self.bad_threshold if threshold is None else threshold)

    def format_value(self, value: Optional[float]) -> str:
        """A mért érték olvasható alakja (órában vagy darabban).

        Hívja: MeasureDef.explain."""
        if value is None:
            return "–"
        if self.kind == "duration":
            return f"{value:.1f}".replace(".", ",")
        return f"{value:g}"

    def explain(self, value: Optional[float], threshold: float, pending: bool = False) -> str:
        """Miért érintett (vagy miért nem) egy eset: a mért érték szavakban, a küszöbbel.

        Hívja: analysis.analyze_candidate."""
        text = (self.pending_reason if pending and self.pending_reason else self.reason).format(value=self.format_value(value))
        limit = f"{threshold:g} {self.unit}".replace(".", ",").strip()
        if self.kind == "flag":
            return text if self.is_bad(value, threshold) else "nem történt meg"
        return f"{text} – küszöb: {limit}" + ("" if self.is_bad(value, threshold) else " (rendben)")


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
