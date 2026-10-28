from datetime import datetime
from typing import Any, Dict, Iterable, List

from config.thresholds import WINDOW, ThresholdManager
from issue_manager import IssueManager
from models.diagnostics.backlog_aging import BacklogAgingDiagnosis
from models.diagnostics.core import Trigger, parse_timestamp
from models.diagnostics.feature_usage import FeatureUsageDiagnosis
from models.diagnostics.flapping import FlappingDiagnosis
from models.diagnostics.issue_trend import IssueTrendDiagnosis
from models.diagnostics.pattern_finding import PatternFindingDiagnosis
from models.diagnostics.ping_pong import PingPongDiagnosis
from models.diagnostics.registry import DiagnosticRegistry
from models.diagnostics.similar_description import SimilarDescriptionDiagnosis
from models.diagnostics.ticket_volume import TicketVolumeDiagnosis
from models.diagnostics.unassigned import UnassignedDiagnosis
from models.incident import Incident
from dataclasses import replace

from models.knowledge_base import KnowledgeBase, KnowledgeView


class DiagnosticsError(RuntimeError):
    """A diagnosztika vagy az issue-egyeztetés hibázott; az incidensek ettől még el vannak mentve."""


class IncidentAnalyzer:
    """Diagnosztika és issue-egyeztetés a memóriában lévő adatbázison (db_data).

    Fájlt nem olvas és nem ír: a betöltést/mentést a hívó végzi a repository tranzakciójában.
    """

    def __init__(self, thresholds_path: str = "database.json.thresholds.json", knowledge_path: str = "knowledge.json"):
        self.issue_manager = IssueManager()
        # A közös tudástár (L1-csoportok, funkció-elérhetőség, ...): a feltárási konzol írja
        self.knowledge = KnowledgeBase(path=knowledge_path)
        # a tanuló kör elvető szabályai a detektoroknak (a feltárási szolgáltatás köti be; nélküle nincs elnyomás)
        self.rule_provider = None
        self.diagnostic_registry = DiagnosticRegistry([
            TicketVolumeDiagnosis(),
            UnassignedDiagnosis(),
            FlappingDiagnosis(),
            PingPongDiagnosis(),
            SimilarDescriptionDiagnosis(),
            BacklogAgingDiagnosis(),
            PatternFindingDiagnosis(),
            FeatureUsageDiagnosis(),
            IssueTrendDiagnosis(),
        ])
        self.threshold_manager = ThresholdManager(
            self.diagnostic_registry.diagnostics,
            storage_path=thresholds_path,
        )
        self.thresholds = self.threshold_manager.config

    # --- ESEMÉNYADAG UTÁNI ELEMZÉS ---

    def analyze_batch(self, db_data: Dict[str, Any], incidents: List[Incident]) -> None:
        """Diagnosztika az összes incidensen; a mérés ideje a köteg legkésőbbi eseménye (virtuális idő)."""
        all_incidents = self._incidents(db_data)
        print(f"--> [ANALYZER] Összes incidens a DB-ben mentés előtt: {len(all_incidents)} db", flush=True)
        self._execute(
            db_data,
            Trigger.EVENT_BATCH,
            all_incidents,
            measured_at=self._latest_event_time(incidents),
            reason="diagnostic_rerun",
            batch_events=[event for incident in incidents for event in incident.history],
        )

    def reanalyze_all(self, db_data: Dict[str, Any]) -> None:
        """Teljes újraelemzés minden incidensen."""
        incidents = self._incidents(db_data)
        if not incidents:
            return
        self._execute(
            db_data,
            Trigger.FULL_REANALYSIS,
            incidents,
            measured_at=self._latest_event_time(incidents),
            reason="full_reanalysis",
        )

    def _execute(self, db_data, trigger, incidents, measured_at, reason, batch_events=()) -> None:
        print(f"--> [ANALYZER] Diagnosztikai elemzés indítása ({trigger}, mérési idő: {measured_at})...", flush=True)
        result = self.diagnostic_registry.execute(
            db_data,
            self.issue_manager,
            trigger=trigger,
            measured_at=measured_at,
            incidents=incidents,
            threshold_map=self.thresholds,
            window_map=self.threshold_manager.window_config,
            reason=reason,
            batch_events=batch_events,
            disabled=self.threshold_manager.disabled,
            knowledge=self._knowledge_view(measured_at),
            review=self.threshold_manager.review_keys,
        )
        print(
            f"--> [ANALYZER] {len(result.runs)} diagnosztika lefutott, {result.measurement_count} mérés, "
            f"{len(result.changes)} issue-állapotváltozás.",
            flush=True,
        )

    # --- DASHBOARD MŰVELETEK ---

    def update_param(self, section: str, config_key: str, key: str, value: Any) -> bool:
        """Küszöb- vagy ablakparaméter módosítása; True, ha az érték ténylegesen megváltozott."""
        return self.threshold_manager.update_param(section, config_key, key, value)

    def apply_param_change(self, db_data: Dict[str, Any], config_key: str, section: str) -> list[dict]:
        """Egy diagnosztika paraméterváltozásának átvezetése a saját issue-ira.

        Utána a 2. fázisú (issue-kra épülő) diagnosztikák is frissülnek, mert az issue-halmaz változhatott.
        """
        diagnostic = self.diagnostic_registry.by_config_key(config_key)
        if diagnostic is None:
            raise KeyError(f"Ismeretlen diagnosztika: {config_key}")
        trigger = Trigger.WINDOW_CHANGED if section == WINDOW else Trigger.THRESHOLD_CHANGED
        reason = "window_changed" if section == WINDOW else "threshold_changed"
        incidents = self._incidents(db_data)
        measured_at = self._latest_event_time(incidents)

        changes = []
        for target in [diagnostic] + self._dependents(diagnostic):
            changes.extend(self.diagnostic_registry.apply_param_change(
                db_data,
                self.issue_manager,
                target,
                trigger=trigger,
                measured_at=measured_at,
                incidents=incidents,
                threshold_map=self.thresholds,
                window_map=self.threshold_manager.window_config,
                reason=reason,
                disabled=self.threshold_manager.disabled,
                knowledge=self._knowledge_view(measured_at),
                review=self.threshold_manager.review_keys,
            ))
        return changes

    def set_diagnostic_enabled(self, db_data: Dict[str, Any], config_key: str, enabled: bool) -> list[dict]:
        """Diagnosztika be- vagy kikapcsolása.

        Kikapcsolva nem fut, és az issue-it a többi diagnosztika sem látja (az issue-k érintetlenek).
        Visszakapcsoláskor azonnal lefut; mindkét esetben az issue-kra épülő diagnosztikák is frissülnek.
        """
        diagnostic = self.diagnostic_registry.by_config_key(config_key)
        if diagnostic is None:
            raise KeyError(f"Ismeretlen diagnosztika: {config_key}")
        if not self.threshold_manager.set_enabled(config_key, enabled):
            return []
        targets = ([diagnostic] if enabled else []) + self._dependents(diagnostic)
        incidents = self._incidents(db_data)
        measured_at = self._latest_event_time(incidents)
        result = self.diagnostic_registry.run_diagnostics(
            db_data,
            self.issue_manager,
            targets,
            trigger=Trigger.MANUAL,
            measured_at=measured_at,
            incidents=incidents,
            threshold_map=self.thresholds,
            window_map=self.threshold_manager.window_config,
            reason="diagnostic_enabled" if enabled else "diagnostic_disabled",
            disabled=self.threshold_manager.disabled,
            knowledge=self._knowledge_view(measured_at),
            review=self.threshold_manager.review_keys,
        )
        return result.changes

    def _dependents(self, diagnostic) -> list:
        """A diagnosztika eredményére épülő (későbbi fázisú), engedélyezett diagnosztikák."""
        disabled = self.threshold_manager.disabled
        return [
            d for d in self.diagnostic_registry.diagnostics
            if d.phase > diagnostic.phase and d.config_key not in disabled
        ]

    def set_diagnostic_review(self, config_key: str, review: bool) -> bool:
        """Jóváhagyásos mód: a diagnosztika jelzései a tanuló körbe mennek, issue (feladat) csak elfogadásból
        lesz. A meglévő issue-k maradnak, és tovább mérődnek."""
        if self.diagnostic_registry.by_config_key(config_key) is None:
            raise KeyError(f"Ismeretlen diagnosztika: {config_key}")
        return self.threshold_manager.set_review(config_key, review)

    def review_context(self, db_data: Dict[str, Any], config_key: str, measured_at: datetime):
        """A jóváhagyásos diagnosztika kontextusa a jelzések begyűjtéséhez és az elfogadáshoz."""
        diagnostic, ctx = self.context_for(db_data, config_key, Trigger.MANUAL, measured_at)
        return diagnostic, ctx

    def apply_issue_action(self, db_data: Dict[str, Any], issue_ids: List[str], action: str) -> list[dict]:
        """Dashboard issue-művelet (pin, suppress, resolve...)."""
        return self.issue_manager.apply_action(db_data, issue_ids, action)

    def context_for(self, db_data: Dict[str, Any], config_key: str, trigger: str, measured_at: datetime):
        """Egy diagnosztika mérési kontextusa a jelenlegi paraméterekkel és tudással (pl. finding előléptetéséhez)."""
        diagnostic = self.diagnostic_registry.by_config_key(config_key)
        if diagnostic is None:
            raise KeyError(f"Ismeretlen diagnosztika: {config_key}")
        return diagnostic, self.diagnostic_registry.context_for_diagnostic(
            db_data, diagnostic, trigger, measured_at, self._incidents(db_data), self.thresholds,
            self.threshold_manager.window_config, self.threshold_manager.disabled, self._knowledge_view(measured_at),
        )

    # --- SEGÉDEK ---

    def _knowledge_view(self, measured_at: datetime) -> KnowledgeView:
        """A mérés pillanatában érvényes tények; a tár újraolvasva, mert a konzol közben írhatta."""
        self.knowledge.reload()
        view = self.knowledge.view(measured_at.isoformat())
        if self.rule_provider is not None:
            try:
                view = replace(view, suppressions=tuple(self.rule_provider()))
            except Exception as error:   # a szabályok olvasása nem akaszthatja meg a mérést
                print(f"--> [ANALYZER] az elvető szabályok nem olvashatók: {error}", flush=True)
        return view

    @staticmethod
    def _incidents(db_data: Dict[str, Any]) -> List[Incident]:
        return [
            incident if isinstance(incident, Incident) else Incident.from_dict(incident)
            for incident in db_data.get("incidents", {}).values()
        ]

    @staticmethod
    def _latest_event_time(incidents: Iterable[Incident]) -> datetime:
        """A virtuális idő: a feldolgozott események közül a legkésőbbi; ha nincs ilyen, a valós idő."""
        latest = None
        for incident in incidents:
            for event in incident.history:
                moment = parse_timestamp(event.timestamp)
                if moment is not None and (latest is None or moment > latest):
                    latest = moment
        return latest or datetime.now()
