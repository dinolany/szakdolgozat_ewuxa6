import copy
import traceback
from collections import OrderedDict
from typing import Any, Dict, List, Tuple

from analyzer import DiagnosticsError, IncidentAnalyzer
from models.history import HistoryLine
from models.incident import Incident
from repository import IncidentRepository


class IncidentEventProcessor:
    """Az eseménycsomagok feldolgozója (szimulációban a PlaybackQueue esedékes csomagjai, élesben az API-s betöltés).

    A bemenet nyers sorok (szótárak) listája; az eseményobjektumokat (HistoryLine) itt hozzuk létre.
    Egy csomagot egyetlen repository-tranzakcióban dolgoz fel:
    HistoryLine-ná alakítás -> csoportosítás incidensenként -> új vagy meglévő incidens
    -> Incident.apply_event -> elemzés -> mentés.
    Azt, hogy egy esemény melyik mezőt hogyan változtatja (és hogy duplikátum-e), az Incident dönti el.
    Élesben ugyanezt hívhatja az API-s betöltés is, a szimulációtól függetlenül.
    """

    def __init__(self, repository: IncidentRepository, analyzer: IncidentAnalyzer, discovery: Any = None):
        self.repository = repository
        self.analyzer = analyzer
        # Feltárás (DiscoveryService): a kötegek után virtuális naponta egyszer fut
        self.discovery = discovery

    def process_events(self, events: List[Any]) -> None:
        if not events:
            print("⚠️ [EVENTS] A process_events üres eseménylistát kapott!", flush=True)
            return

        print(f"--> [EVENTS] process_events meghívva {len(events)} darab eseménnyel.", flush=True)

        history_lines, invalid_count = self._to_history_lines(events)
        if invalid_count:
            print(f"⚠️ [EVENTS] {invalid_count} db érvénytelen sor kihagyva (hiányzó incidens azonosító vagy időbélyeg).", flush=True)

        grouped = self._group_by_incident(history_lines)
        if not grouped:
            print("❌ [EVENTS] Egyetlen érvényes incidens ID sem akadt horogra az eseményekben!", flush=True)
            return

        analysis_error = None
        with self.repository.transaction() as db_data:
            affected_incidents, duplicate_count = self._apply_events(db_data, grouped)
            if duplicate_count:
                print(f"--> [EVENTS] {duplicate_count} db már feldolgozott esemény kihagyva (duplikáció).", flush=True)

            # Ha az elemzés félúton elhasal, az issue-k ne maradjanak félig egyeztetett állapotban
            issues_before = copy.deepcopy(db_data["issues"])
            history_before = list(db_data["issue_history"])
            try:
                self.analyzer.analyze_batch(db_data, affected_incidents)
            except Exception as error:
                traceback.print_exc()
                db_data["issues"] = issues_before
                db_data["issue_history"] = history_before
                analysis_error = error
            if analysis_error is None and self.discovery is not None:
                # A feltárás hibája nem akaszthatja meg az eseményfeldolgozást (a tanuló kör külön szál)
                try:
                    report = self.discovery.after_batch(db_data, self.analyzer._latest_event_time(affected_incidents))
                    if report:
                        print(f"--> [FELTÁRÁS] {report['measured_at']}: {report['new']} új, {report['updated']} ismert jelölt", flush=True)
                except Exception:
                    traceback.print_exc()
            # A tranzakció végén a repository ment (az incidenseket az elemzés hibája esetén is)

        if analysis_error is not None:
            message = f"A diagnosztika hibázott ({analysis_error}); az incidensek mentve, az issue-k nem frissültek."
            print(f"❌ [EVENTS] {message}", flush=True)
            raise DiagnosticsError(message) from analysis_error

        print(f"--> [EVENTS] Sikeresen feldolgozva és lementve {len(affected_incidents)} db incidens.", flush=True)

    @staticmethod
    def _to_history_lines(events: List[Any]) -> Tuple[List[HistoryLine], int]:
        """Nyers sorokból HistoryLine; az érvénytelen sor kimarad (nem dönti el az egész csomagot)."""
        history_lines: List[HistoryLine] = []
        invalid_count = 0
        for event in events:
            if isinstance(event, HistoryLine):
                history_lines.append(event)
                continue
            try:
                history_lines.append(HistoryLine(event))
            except ValueError:
                invalid_count += 1
        return history_lines, invalid_count

    @staticmethod
    def _group_by_incident(history_lines: List[HistoryLine]) -> "OrderedDict[str, List[HistoryLine]]":
        """Incidensenkénti csoportok, az események eredeti (időrendi) sorrendjében."""
        grouped: "OrderedDict[str, List[HistoryLine]]" = OrderedDict()
        for history_line in history_lines:
            inc_id = str(history_line.set).strip()
            if inc_id in ("", "None", "UNKNOWN"):
                continue
            grouped.setdefault(inc_id, []).append(history_line)
        return grouped

    @staticmethod
    def _apply_events(db_data: Dict[str, Any], grouped: Dict[str, List[HistoryLine]]) -> Tuple[List[Incident], int]:
        """Meglévő incidensre alkalmazza az eseményeket, vagy újat hoz létre; visszaadja az érintetteket."""
        affected: List[Incident] = []
        duplicate_count = 0
        for inc_id, history_lines in grouped.items():
            # A meglévő incidenst a repository már Incident objektumként töltötte be
            incident = db_data["incidents"].get(inc_id)
            if incident is None:
                incident = Incident(incident_id=inc_id)

            for history_line in history_lines:
                if incident.apply_event(history_line).get("duplicate"):
                    duplicate_count += 1

            db_data["incidents"][inc_id] = incident
            affected.append(incident)
        return affected, duplicate_count
