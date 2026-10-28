"""Ticket-nyitási volumen: az utolsó lezárult órás vödör nyitásai a korábbi napok azonos órájához képest.

A várható érték a vödör előtti időszak napi átlaga ugyanabban az órában, hétköznap és hétvége
külön (a hétvégi forgalom töredéke a hétköznapinak). A nyitás ideje a ticket első eseménye.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from models.diagnostics.core import (
    BUCKET_KEY,
    CrossTicketDiagnosis,
    MeasurementContext,
    MeasurementWindow,
    Observation,
    ParameterDef,
    WindowType,
)
from models.diagnostics.history_refs import make_history_ref
from models.issue import SeverityLevel
from models.timeline import incident_timeline


def _day_type(moment: datetime) -> str:
    return "weekend" if moment.weekday() >= 5 else "weekday"


class TicketVolumeDiagnosis(CrossTicketDiagnosis):
    key = "TICKET_VOLUME_DEVIATION"
    config_key = "ticket_volume"
    issue_category = "CROSS_TICKET"
    window_type = WindowType.TUMBLING
    metric_key = "deviation_percent"
    metric_label = "eltérés (%)"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("minimum_openings", "Minimum ticket szám", 5, 1, 100000, 1,
                         "Ennél kevesebb nyitásnál a vödör nem vizsgált.", "db"),
            ParameterDef("moderate_deviation_percent", "Mérsékelt eltérés", 50, 0, 10000, 1,
                         "A várhatónál ennyivel több nyitás aktív issue.", "%"),
            ParameterDef("extreme_deviation_percent", "Extrém eltérés", 100, 0, 10000, 1, "Extrém súlyossági szint küszöbe.", "%"),
            ParameterDef("minimum_history_days", "Minimum előzmény", 3, 1, 365, 1,
                         "Ennyi azonos típusú (hétköznap/hétvége) korábbi nap kell a várható értékhez.", "nap"),
        ]

    @classmethod
    def window_definitions(cls) -> List[ParameterDef]:
        return [ParameterDef(BUCKET_KEY, "Vödör", 60, 15, 1440, 15, "A vizsgált időszelet hossza.", "perc")]

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        if details.get("openings", 0) < thresholds["minimum_openings"] or details.get("history_days", 0) < thresholds["minimum_history_days"]:
            return None
        deviation = details.get("deviation_percent", 0)
        if deviation >= thresholds["extreme_deviation_percent"]:
            return SeverityLevel.EXCEEDED_EXTREME
        if deviation >= thresholds["moderate_deviation_percent"]:
            return SeverityLevel.EXCEEDED_MODERATE
        return None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        return (f"{details.get('openings', 0)} ticket nyílt; a korábbi {details.get('history_days', 0)} nap alapján "
                f"{details.get('expected', 0):.1f} volt várható ({details.get('deviation_percent', 0):+.0f}%).")

    def observe_incidents(self, incidents, window, ctx):  # a _observe felülírása miatt nem használt
        return ()

    def _observe(self, ctx: MeasurementContext, window: MeasurementWindow) -> Iterable[Observation]:
        if window.start is None or window.end is None:
            return []
        opened: List[Tuple[str, datetime, Any]] = []
        for incident in ctx.incidents:
            timeline = incident_timeline(incident, ctx.measured_at)
            if timeline is not None:
                opened.append((timeline.incident_id, timeline.opened_at, timeline.changes[0].event))
        in_bucket = [(i, at, event) for i, at, event in opened if window.start <= at < window.end]

        # a vödör előtti napok azonos órás szelete, azonos naptípuson
        day_type, start_time, end_time = _day_type(window.start), window.start.time(), window.end.time()
        history_days: Set[str] = set()
        per_day: Dict[str, int] = defaultdict(int)
        earliest = min((at for _, at, _ in opened), default=window.start)
        day = earliest.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < window.start.replace(hour=0, minute=0, second=0, microsecond=0):
            if _day_type(day) == day_type:
                history_days.add(day.date().isoformat())
            day += timedelta(days=1)
        for _, at, _ in opened:
            if at < window.start and at.date().isoformat() in history_days and start_time <= at.time() < (end_time if end_time > start_time else datetime.max.time()):
                per_day[at.date().isoformat()] += 1
        expected = sum(per_day.values()) / len(history_days) if history_days else 0.0
        deviation = ((len(in_bucket) - expected) / expected) * 100 if expected else 0.0
        details = {
            "openings": len(in_bucket), "expected": round(expected, 2), "deviation_percent": round(deviation, 1),
            "history_days": len(history_days), "day_type": day_type,
        }
        return [Observation(
            subject="openings",
            details=details,
            affected=tuple(sorted(i for i, _, _ in in_bucket)),
            history_refs=tuple(make_history_ref(i, event, "ticket_opening_volume") for i, _, event in in_bucket),
        )]
