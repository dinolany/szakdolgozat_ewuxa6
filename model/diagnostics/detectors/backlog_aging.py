"""Öregedő backlog csoportonként: a csoportnál régóta, aktívan várakozó nyitott ticketek.

A history-ból, a mérés pillanatában (nem az incidens éppen beírt mezőiből): minden nyitott ticket
annál a csoportnál számít, ahol most van, és a várakozás a csoporthoz érkezés óta eltelt idő a
hold (felfüggesztett) idő nélkül, mert az a csoportra vonatkozó mérés (nem a ticket teljes kora).
"""

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core import CrossTicketDiagnosis, MeasurementContext, MeasurementWindow, Observation, ParameterDef, WindowType
from model.diagnostics.history_refs import make_history_ref
from model.severity_level import SeverityLevel
from model.timeline.stint import Stint
from model.timeline.ticket_timeline import TicketTimeline
from model.timeline.timeline_builder import incident_timeline


class BacklogAgingDiagnosis(CrossTicketDiagnosis):
    key = "BACKLOG_AGING"
    config_key = "backlog_aging"
    issue_category = "BACKLOG_AGING"
    window_type = WindowType.SNAPSHOT
    metric_key = "aged"
    metric_label = "öregedő ticket"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("moderate_age_hours", "Mérsékelt várakozás", 24, 1, 8760, 1,
                         "Ennyi óra aktív várakozás (a csoporthoz érkezés óta, hold nélkül) után öregedő a ticket; munkaórában, "
                         "ha a csoport vagy a cég munkaideje ismert (Tudástár › munkaido), különben faliórában.", "óra"),
            ParameterDef("extreme_age_hours", "Extrém várakozás", 72, 1, 8760, 1,
                         "Ha van ennél régebben várakozó ticket, extrém.", "óra"),
            ParameterDef("minimum_count", "Legkevesebb öregedő ticket", 2, 1, 10000, 1,
                         "Csoportonként legalább ennyi öregedő ticket kell az aktív issue-hoz.", "db"),
        ]

    signal_label = "Öregedő backlog"

    def signal_items(self, subject: str, details: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
        return (("assignment_group", subject),)

    def issue_id_for(self, occurrence: str) -> str:
        return f"ISSUE-{self.key}-{occurrence.replace(' ', '_').upper()}"  # a régi formátum

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        if details.get("aged", 0) < thresholds["minimum_count"]:
            return None
        if details.get("oldest_hours", 0) >= thresholds["extreme_age_hours"]:
            return SeverityLevel.EXCEEDED_EXTREME
        return SeverityLevel.EXCEEDED_MODERATE

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        group = details.get("group_label", subject)
        unit, than = ("munkaóra", "munkaóránál") if details.get("time_basis") == "munkaóra" else ("óra", "óránál")
        return (f"{group}: {details.get('aged', 0)} nyitott ticket várakozik {details.get('threshold_hours', 0):g} {than} "
                f"régebben (legrégebbi {details.get('oldest_hours', 0):.0f} {unit}, nyitott összesen {details.get('open', 0)}).")

    @staticmethod
    def _age(stint: Stint, ctx: MeasurementContext) -> Tuple[float, bool]:
        """A szakasz aktív várakozása (a csoporthoz érkezés óta, hold nélkül): munkaórában, ha a csoport (vagy a cég)
        munkaideje ismert, különben faliórában. (kor, munkaórában-e)"""
        schedules = getattr(ctx.knowledge, "schedules", None) or {}
        schedule = schedules.get(stint.group)
        if schedule is None:
            return stint.hours - stint.hold_hours, False
        hold = sum(schedule.working_hours(h.start, h.end) for h in stint.holds)
        return schedule.working_hours(stint.start, stint.end) - hold, True

    def child_units(self, ctx: MeasurementContext) -> Dict[str, Tuple[List[Tuple[str, Optional[int]]], List[Tuple[str, Optional[int]]]]]:
        """Almérésekhez csoportonként: az öregedő nyitott szakaszok és a csoport többi nyitott szakasza."""
        threshold = self.thresholds(ctx)["moderate_age_hours"]
        units: Dict[str, Tuple[List[Tuple[str, Optional[int]]], List[Tuple[str, Optional[int]]]]] = {}
        for incident in ctx.incidents:
            timeline = incident_timeline(incident, ctx.measured_at)
            if timeline is None or timeline.is_closed or not timeline.stints or not timeline.stints[-1].is_open:
                continue
            stint = timeline.stints[-1]
            aged, other = units.setdefault(stint.group, ([], []))
            (aged if self._age(stint, ctx)[0] >= threshold else other).append((timeline.incident_id, stint.index))
        return units

    def observe_incidents(self, incidents, window, ctx):  # a _observe felülírása miatt nem használt
        return ()

    def _observe(self, ctx: MeasurementContext, window: MeasurementWindow) -> Iterable[Observation]:
        threshold = self.thresholds(ctx)["moderate_age_hours"]
        waiting: Dict[str, List[Tuple[TicketTimeline, Stint, float]]] = defaultdict(list)
        working: Dict[str, bool] = {}
        for incident in ctx.incidents:
            timeline = incident_timeline(incident, ctx.measured_at)
            if timeline is None or timeline.is_closed or not timeline.stints or not timeline.stints[-1].is_open:
                continue
            stint = timeline.stints[-1]
            age, in_working_hours = self._age(stint, ctx)
            waiting[stint.group].append((timeline, stint, age))
            working[stint.group] = in_working_hours

        levels = ctx.knowledge.levels
        for group, tickets in sorted(waiting.items()):
            aged = sorted((item for item in tickets if item[2] >= threshold), key=lambda item: -item[2])
            details = {
                "group": group,
                "group_label": f"{group} ({levels[group]})" if group in levels else group,
                "open": len(tickets),
                "aged": len(aged),
                "oldest_hours": aged[0][2] if aged else 0.0,
                "threshold_hours": threshold,
                "time_basis": "munkaóra" if working.get(group) else "falióra",
                "aged_incidents": [{"incident_id": t.incident_id, "hours": round(h, 1)} for t, _, h in aged[:50]],
            }
            yield Observation(
                subject=group,
                details=details,
                affected=tuple(t.incident_id for t, _, _ in aged),
                history_refs=tuple(make_history_ref(t.incident_id, s.changes[0].event, "backlog_age") for t, s, _ in aged[:50]),
            )
