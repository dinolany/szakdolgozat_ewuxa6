from dataclasses import replace
from datetime import datetime
from typing import Any, Collection, Dict, Iterable, List, Mapping, Optional, Sequence

from model.diagnostics.core import BaseDiagnosis, MeasurementContext
from model.diagnostics.registry_result import RegistryResult
from model.history_line import HistoryLine
from model.incident import Incident
from model.issue import Issue
from model.knowledge.knowledge_view import KnowledgeView


class DiagnosticRegistry:
    """A diagnosztikák nyilvántartása és futtatása.

    - trigger szerint választ, fázisonként fut (1: ticket-diagnosztikák, 2: issue-diagnosztikák)
    - minden diagnosztika a saját (config_key szerinti) küszöbeit és ablakparamétereit kapja
    - minden futás után azonnal átvezeti az eredményt a diagnosztika saját issue-ira,
      így a 2. fázis már a frissített issue-kat látja
    - a kikapcsolt diagnosztikák (disabled: config_key-ek) nem futnak, és az issue-ik
      a többi diagnosztika számára sem látszanak; az issue-k maguk érintetlenek maradnak
    """

    def __init__(self, diagnostics: Iterable[Any] = ()):
        self._diagnostics: List[BaseDiagnosis] = []
        for diagnostic in diagnostics:
            self.register(diagnostic)

    def register(self, diagnostic: Any) -> BaseDiagnosis:
        if not isinstance(diagnostic, BaseDiagnosis):
            raise TypeError(f"Nem core diagnosztika: {type(diagnostic).__name__} (model.diagnostics.core alaposztály kell)")
        for existing in self._diagnostics:
            if existing.key == diagnostic.key:
                raise ValueError(f"Már regisztrált diagnosztika: {diagnostic.key}")
            if existing.config_key == diagnostic.config_key:
                raise ValueError(f"Ütköző config_key: {diagnostic.config_key} ({existing.key} / {diagnostic.key})")
        self._diagnostics.append(diagnostic)
        return diagnostic

    @property
    def diagnostics(self) -> tuple[BaseDiagnosis, ...]:
        return tuple(self._diagnostics)

    def get(self, key: str) -> Optional[BaseDiagnosis]:
        return next((d for d in self._diagnostics if d.key == key), None)

    def by_config_key(self, config_key: str) -> Optional[BaseDiagnosis]:
        return next((d for d in self._diagnostics if d.config_key == config_key), None)

    def for_trigger(self, trigger: str, disabled: Collection[str] = ()) -> List[BaseDiagnosis]:
        selected = [d for d in self._diagnostics if trigger in d.triggers and d.config_key not in disabled]
        return sorted(selected, key=lambda d: d.phase)  # stabil rendezés: fázison belül a regisztráció sorrendje

    def disabled_keys(self, disabled: Collection[str]) -> frozenset[str]:
        """A kikapcsolt diagnosztikák diagnostic_key-ei (config_key-ből)."""
        return frozenset(d.key for d in self._diagnostics if d.config_key in disabled)

    # ------------------------------------------------------------------

    def execute(
        self,
        db_data: Dict[str, Any],
        issue_manager: Any,
        trigger: str,
        measured_at: datetime,
        incidents: Sequence[Incident],
        threshold_map: Mapping[str, Mapping[str, Any]],
        window_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
        reason: str = "diagnostic_run",
        batch_events: Sequence[HistoryLine] = (),
        disabled: Collection[str] = (),
        knowledge: Optional[KnowledgeView] = None,
        review: Collection[str] = (),
    ) -> RegistryResult:
        """A triggerhez tartozó (engedélyezett) diagnosztikák lefuttatása és az issue-k átvezetése."""
        return self.run_diagnostics(
            db_data, issue_manager, self.for_trigger(trigger, disabled), trigger, measured_at,
            incidents, threshold_map, window_map, reason, batch_events, disabled, knowledge, review,
        )

    def run_diagnostics(
        self,
        db_data: Dict[str, Any],
        issue_manager: Any,
        diagnostics: Iterable[BaseDiagnosis],
        trigger: str,
        measured_at: datetime,
        incidents: Sequence[Incident],
        threshold_map: Mapping[str, Mapping[str, Any]],
        window_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
        reason: str = "diagnostic_run",
        batch_events: Sequence[HistoryLine] = (),
        disabled: Collection[str] = (),
        knowledge: Optional[KnowledgeView] = None,
        review: Collection[str] = (),
    ) -> RegistryResult:
        """Megadott diagnosztikák normál futtatása fázissorrendben (trigger-szűrés nélkül).

        `review`: a jóváhagyásos módú diagnosztikák (config_key): ezek nem nyitnak új issue-t.
        """
        result = RegistryResult()
        selected = sorted(diagnostics, key=lambda d: d.phase)
        for phase in sorted({d.phase for d in selected}):
            base_ctx = self._base_context(db_data, trigger, measured_at, incidents, disabled, batch_events, knowledge)
            for diagnostic in (d for d in selected if d.phase == phase):
                ctx = self._context_for(diagnostic, base_ctx, threshold_map, window_map)
                run = diagnostic.run(replace(ctx, review=diagnostic.config_key in review))
                result.runs.append(run)
                result.changes.extend(issue_manager.apply_run(db_data, run, reason))
        return result

    def apply_param_change(
        self,
        db_data: Dict[str, Any],
        issue_manager: Any,
        diagnostic: BaseDiagnosis,
        trigger: str,
        measured_at: datetime,
        incidents: Sequence[Incident],
        threshold_map: Mapping[str, Mapping[str, Any]],
        window_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
        reason: str = "threshold_changed",
        disabled: Collection[str] = (),
        knowledge: Optional[KnowledgeView] = None,
        review: Collection[str] = (),
    ) -> List[dict]:
        """Egyetlen diagnosztika issue-inak frissítése paraméterváltozás után (trigger-szűrés nélkül)."""
        if diagnostic.config_key in disabled:
            return []  # kikapcsolt diagnosztika: a paraméter elmentődik, de mérés nincs
        base_ctx = self._base_context(db_data, trigger, measured_at, incidents, disabled, knowledge=knowledge)
        ctx = replace(self._context_for(diagnostic, base_ctx, threshold_map, window_map),
                      review=diagnostic.config_key in review)
        return issue_manager.apply_param_change(db_data, diagnostic, ctx, reason)

    def context_for_diagnostic(
        self,
        db_data: Dict[str, Any],
        diagnostic: BaseDiagnosis,
        trigger: str,
        measured_at: datetime,
        incidents: Sequence[Incident],
        threshold_map: Mapping[str, Mapping[str, Any]],
        window_map: Optional[Mapping[str, Mapping[str, Any]]] = None,
        disabled: Collection[str] = (),
        knowledge: Optional[KnowledgeView] = None,
    ) -> MeasurementContext:
        """Egy diagnosztika mérési kontextusa (saját küszöbök, ablak, tudástár), pl. célzott méréshez."""
        base_ctx = self._base_context(db_data, trigger, measured_at, incidents, disabled, knowledge=knowledge)
        return self._context_for(diagnostic, base_ctx, threshold_map, window_map)

    def _base_context(self, db_data, trigger, measured_at, incidents, disabled, batch_events=(),
                      knowledge: Optional[KnowledgeView] = None) -> MeasurementContext:
        hidden = self.disabled_keys(disabled)
        return MeasurementContext(
            trigger=trigger,
            measured_at=measured_at,
            incidents=incidents,
            issues=[issue for issue in self._current_issues(db_data) if issue.diagnostic_key not in hidden],
            batch_events=batch_events,
            knowledge=knowledge or KnowledgeView(),
        )

    @staticmethod
    def _context_for(diagnostic, base_ctx, threshold_map, window_map) -> MeasurementContext:
        return base_ctx.for_diagnostic(
            (threshold_map or {}).get(diagnostic.config_key, {}),
            (window_map or {}).get(diagnostic.config_key, {}),
        )

    @staticmethod
    def _current_issues(db_data: Dict[str, Any]) -> List[Issue]:
        return [
            issue if isinstance(issue, Issue) else Issue.from_dict(issue)
            for issue in db_data.get("issues", {}).values()
        ]
