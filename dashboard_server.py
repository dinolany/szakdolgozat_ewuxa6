import json
import math
import numbers
import threading
import traceback
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from analyzer import IncidentAnalyzer
from config.thresholds import THRESHOLDS
from models.discovery.service import DiscoveryService
from repository import IncidentRepository, clean_nan


def clean_nan(value: Any):
    """JSON-kompatibilissé tisztítja a DTO-k hiányzó és nem véges értékeit."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, numbers.Real):
        numeric_value = float(value)
        return numeric_value if math.isfinite(numeric_value) else None
    if value.__class__.__name__ in {"NaTType", "NAType"}:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: clean_nan(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean_nan(item) for item in value]
    if hasattr(value, "item"):
        try:
            return clean_nan(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "to_dict"):
        return clean_nan(value.to_dict())
    return str(value)


class DashboardService:
    SIMULATION_DURATIONS = {600, 3600, 0}

    def __init__(self, db_path: str = "database.json", scheduler=None, repository=None, analyzer=None, discovery=None):
        self.db_path = Path(db_path)
        # Ugyanazokat a példányokat kell kapnia, mint az eseményfeldolgozónak, különben
        # két író felülírja egymást, és a küszöbváltozást az elemzés nem látná
        self.repo = repository or IncidentRepository(str(self.db_path))
        self.analyzer = analyzer or IncidentAnalyzer(f"{self.db_path}.thresholds.json")
        self.thresholds = self.analyzer.threshold_manager
        self.scheduler = scheduler  # SimulationScheduler: a szimuláció egyetlen vezérlője
        # A feltárási ("tanuló") kör: ugyanaz a példány, amelyet a szimuláció kötegei is futtatnak
        self.discovery = discovery or DiscoveryService(
            self.analyzer, self.repo, str(self.db_path.with_name("discovery_state.json")),
            str(self.db_path.with_name("config") / "discovery.json"))
        self._lock = threading.RLock()

    def _read_db(self) -> Dict[str, Any]:
        try:
            return json.loads(self.db_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"incidents": {}, "issues": {}}

    @staticmethod
    def _page(items, page: int, page_size: int):
        page = max(page, 1)
        page_size = min(max(page_size, 1), 100)
        start = (page - 1) * page_size
        values = items[start : start + page_size]
        return values, {
            "page": page,
            "page_size": page_size,
            "total": len(items),
            "pages": max((len(items) + page_size - 1) // page_size, 1),
        }

    def summary(self):
        with self._lock:
            db = self._read_db()
        incidents = list(db.get("incidents", {}).values())
        # A kikapcsolt diagnosztikák issue-i nem számítanak bele az összesítőbe
        hidden = self._disabled_diagnostic_keys()
        all_issues = list(db.get("issues", {}).values())
        issues = [issue for issue in all_issues if issue.get("diagnostic_key") not in hidden]
        status_counts = {key: 0 for key in ("open", "closed", "suspended")}
        for incident in incidents:
            status_counts[self._state_group(incident)] += 1
        category_counts: Dict[str, int] = {}
        issue_state_counts: Dict[str, int] = {}
        for issue in issues:
            category = str(issue.get("category", "UNKNOWN"))
            category_counts[category] = category_counts.get(category, 0) + 1
            state = str(issue.get("state", "active"))
            issue_state_counts[state] = issue_state_counts.get(state, 0) + 1
        return {
            "incidents": len(incidents),
            "issues": len(issues),
            "statuses": status_counts,
            "categories": category_counts,
            "issue_states": issue_state_counts,
            "hidden_issues": len(all_issues) - len(issues),
        }

    def _disabled_diagnostic_keys(self) -> frozenset:
        registry = self.analyzer.diagnostic_registry
        return registry.disabled_keys(self.thresholds.disabled)

    @staticmethod
    def _state_group(incident: Dict[str, Any]) -> str:
        ms = incident.get("main_state")
        if isinstance(ms, dict):
            state = str(ms.get("State (Állapot)", "")).lower()
        elif isinstance(ms, str):
            state = ms.lower()
        else:
            cs = incident.get("current_state")
            if isinstance(cs, dict):
                state = str(cs.get("State (Állapot)", "")).lower()
            elif isinstance(cs, str):
                state = cs.lower()
            else:
                state = ""

        if state in {"6", "7", "closed", "resolved", "zárt", "lezárt"}:
            return "closed"
        if state in {"3", "on hold", "suspended", "felfüggesztve"}:
            return "suspended"
        return "open"

    def incidents(self, query: Dict[str, Any]):
        db = self._read_db()
        values = []
        status = query.get("status", [""])[0]
        search = str(query.get("search", [""])[0]).strip().lower()
        group = str(query.get("group", [""])[0]).strip().lower()
        
        for incident_id, incident in db.get("incidents", {}).items():
            if status and self._state_group(incident) != status:
                continue
                
            state_obj = incident.get("main_state") or incident.get("current_state") or {}
            if isinstance(state_obj, dict):
                state_val = state_obj.get("State (Állapot)", "")
                assignment_group = state_obj.get("Assignment Group", "")
                assigned_to = state_obj.get("Assigned To", "")
                priority = state_obj.get("Priority", "")
                category = state_obj.get("Category", "")
                opened_at = state_obj.get("Opened At", "")
            else:
                state_val = str(state_obj) if state_obj else ""
                assignment_group = incident.get("assignment_group", "")
                assigned_to = incident.get("assigned_to", "")
                priority = incident.get("priority", "")
                category = incident.get("category", "")
                opened_at = incident.get("opened_at", "")

            dto = {
                "incident_id": incident.get("incident_id", incident_id),
                "state": state_val,
                "assignment_group": assignment_group,
                "assigned_to": assigned_to,
                "priority": priority,
                "category": category,
                "history_count": incident.get("history_count", 0),
                "status_group": self._state_group(incident),
                "opened_at": opened_at,
                "updated_at": incident.get("updated_at", ""),
            }
            haystack = " ".join(str(value) for value in dto.values()).lower()
            if search and search not in haystack:
                continue
            if group and group not in str(dto["assignment_group"]).lower():
                continue
            values.append(dto)
            
        values.sort(key=lambda item: item["incident_id"])
        if query.get("all", [""])[0] in {"1", "true", "yes"}:
            return {"items": values, "pagination": {"page": 1, "page_size": len(values), "total": len(values), "pages": 1}}
        page, meta = self._page(
            values,
            int(query.get("page", [1])[0]),
            int(query.get("page_size", [25])[0]),
        )
        return {"items": page, "pagination": meta}

    def incident_detail(self, incident_id: str):
        db = self._read_db()
        incident = db.get("incidents", {}).get(incident_id)
        if incident is None:
            return None
            
        def extract_field(*keys):
            for source in [
                incident.get("main_state"),
                incident.get("current_state"),
                incident,
            ]:
                if isinstance(source, dict):
                    for k in keys:
                        for sk, sv in source.items():
                            if k.lower() in sk.lower() and sv:
                                return sv
            return "-"

        state_val = extract_field("state", "állapot")
        assignment_group = extract_field("assignment_group", "csoport")
        assigned_to = extract_field("assigned_to", "felelős", "assigned")
        priority = extract_field("priority", "prioritás")
        severity = extract_field("severity", "súlyosság")
        urgency = extract_field("urgency", "sürgősség")
        impact = extract_field("impact", "hatás")
        opened_at = extract_field("opened_at", "opened", "nyitás")
        closed_at = extract_field("closed_at", "closed", "zárás")
        resolved_at = extract_field("resolved_at", "resolved", "megoldva")
        category = extract_field("category", "kategória")
        subcategory = extract_field("subcategory", "alkategória")
        short_description = extract_field("short_description", "description", "leírás")
        close_code = extract_field("close_code", "zárási kód")

        return {
            "incident_id": incident.get("incident_id", incident_id),
            "state": state_val if not isinstance(state_val, dict) else state_val.get("State (Állapot)", "-"),
            "assignment_group": assignment_group,
            "assigned_to": assigned_to,
            "priority": priority,
            "severity": severity,
            "urgency": urgency,
            "impact": impact,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "resolved_at": resolved_at,
            "category": category,
            "subcategory": subcategory,
            "short_description": short_description,
            "close_code": close_code,
            "history": incident.get("history_lines") or incident.get("history") or [],
            "history_count": incident.get("history_count", 0),
            "updated_at": incident.get("updated_at", ""),
        }

    def issues(self, query: Dict[str, Any]):
        db = self._read_db()
        category = query.get("category", [""])[0]
        state_filter = query.get("state", [""])[0]
        diagnostic_filter = query.get("diagnostic", [""])[0]
        severity_filter = query.get("severity", [""])[0]
        search = str(query.get("search", [""])[0]).strip().lower()
        include_deleted = query.get("include_deleted", [""])[0] in {"1", "true", "yes"}
        hidden = self._disabled_diagnostic_keys()
        values = []
        for issue_id, issue in db.get("issues", {}).items():
            if issue.get("is_deleted", False) and not include_deleted:
                continue
            if category and str(issue.get("category", "UNKNOWN")) != category:
                continue
            state = str(issue.get("state", "active"))
            if state_filter and state != state_filter:
                continue
            diagnostic = issue.get("diagnostic_key", issue.get("category", "UNKNOWN"))
            severity = issue.get("severity", "")
            if diagnostic_filter and diagnostic != diagnostic_filter:
                continue
            if severity_filter and severity != severity_filter:
                continue
            dto = {
                "issue_id": issue.get("issue_id", issue_id),
                "diagnostic": diagnostic,
                "diagnostic_enabled": diagnostic not in hidden,
                "category": issue.get("category", "UNKNOWN"),
                "severity": severity,
                "state": state,
                "is_pinned": bool(issue.get("is_pinned", False)),
                "is_deleted": bool(issue.get("is_deleted", False)),
                "metric_value": issue.get("metric_value", 0),
                "group_count": issue.get("group_count", issue.get("metric_value", 0)),
                "reassignment_count": issue.get("reassignment_count", 0),
                "groups": issue.get("groups", []),
                "affected_incidents": issue.get("affected_incidents", []),
                "history_ref_count": len(issue.get("history_refs", [])),
                "subject": issue.get("subject", ""),
                "trigger": issue.get("trigger", ""),
                "measured_at": issue.get("measured_at", ""),
                "first_detected_at": issue.get("first_detected_at", ""),
                "window_type": issue.get("window_type", ""),
                "window_start": issue.get("window_start", ""),
                "window_end": issue.get("window_end", ""),
                "params_changed": bool(issue.get("params_changed", False)),
                "description": issue.get("description", ""),
                "updated_at": issue.get("updated_at", ""),
                # funkcióhasználati kérdéseknél: ismert-e az elérhetőség (a Tanulás nézet kérdéslistájához)
                "availability": (issue.get("details") or {}).get("availability", ""),
            }
            if search and search not in json.dumps(dto, ensure_ascii=False).lower():
                continue
            values.append(dto)
        values.sort(key=lambda item: item["issue_id"])
        if query.get("all", [""])[0] in {"1", "true", "yes"}:
            return {"items": values, "pagination": {"page": 1, "page_size": len(values), "total": len(values), "pages": 1}}
        page, meta = self._page(
            values,
            int(query.get("page", [1])[0]),
            int(query.get("page_size", [25])[0]),
        )
        return {"items": page, "pagination": meta}

    def issue_detail(self, issue_id: str):
        db = self._read_db()
        issue = db.get("issues", {}).get(issue_id)
        if issue is None:
            return None
        result = dict(issue)
        result.setdefault("diagnostic", result.get("diagnostic_key", result.get("category", "UNKNOWN")))
        result.setdefault("is_pinned", False)
        result.setdefault("is_deleted", False)
        result["state_history"] = [
            entry
            for entry in db.get("issue_history", [])
            if entry.get("issue_id") == issue_id
        ]
        return result

    def get_thresholds(self):
        return self._thresholds_with_custom()

    def _thresholds_with_custom(self):
        """A küszöb-séma, detektoronként az egyéni (kézi vagy tanult, szűkebb hatókörű) küszöbökkel. Ezek itt nem
        írhatók át: csak a szabály (felülírás) törlésével szűnnek meg."""
        schema = self.thresholds.get_ui_schema()
        knowledge = getattr(self.analyzer, "knowledge", None) if self.analyzer is not None else None
        overrides = [o for o in (knowledge.threshold_overrides() if knowledge is not None else []) if o.state == "active"]
        for category in schema.values():
            if isinstance(category, dict):
                category["custom"] = [{**o.to_dict(), "label": o.label()} for o in overrides
                                      if o.diagnostic == category.get("diagnostic_key")]
        return schema

    def get_threshold_schema(self):
        return self.thresholds.get_ui_schema()

    def update_threshold(self, payload: Dict[str, Any]):
        """Küszöb (section=thresholds) vagy mérésiablak-paraméter (section=window) módosítása."""
        section = str(payload.get("section", THRESHOLDS))
        config_key = str(payload.get("config_key") or payload["category"])
        key = str(payload["key"])
        value = payload["value"]
        changes = []
        # Egyetlen tranzakcióban: a paraméter és az issue-k nem kerülhetnek eltérő állapotba egy futó elemzéssel
        schema = self.thresholds.get_ui_schema()
        old_value = schema.get(config_key, {}).get(section, {}).get(key, {}).get("value")
        with self.repo.transaction() as db:
            if self.analyzer.update_param(section, config_key, key, value):
                try:
                    changes = self.analyzer.apply_param_change(db, config_key, section)
                except Exception:
                    # Az issue-k nem frissültek (a tranzakció nem ment), így a paraméter sem maradhat az új értéken
                    self.analyzer.update_param(section, config_key, key, old_value)
                    raise
        return {
            "thresholds": self._thresholds_with_custom(),
            "reconciled": changes,
            "reconciled_count": len(changes),
        }

    def toggle_diagnostic(self, payload: Dict[str, Any]):
        """Diagnosztika be- vagy kikapcsolása (config_key, enabled)."""
        config_key = str(payload["config_key"])
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Az 'enabled' mező csak true vagy false lehet.")
        with self.repo.transaction() as db:
            previous = self.thresholds.is_enabled(config_key)
            try:
                changes = self.analyzer.set_diagnostic_enabled(db, config_key, enabled)
            except Exception:
                # Az issue-k nem frissültek (a tranzakció nem ment), így a kapcsoló sem maradhat átállítva
                self.thresholds.set_enabled(config_key, previous)
                raise
        return {
            "thresholds": self._thresholds_with_custom(),
            "reconciled": changes,
            "reconciled_count": len(changes),
        }

    def set_review(self, payload: Dict[str, Any]):
        """Jóváhagyásos mód (config_key, review): a diagnosztika jelzései a tanuló körbe mennek."""
        review = payload.get("review")
        if not isinstance(review, bool):
            raise ValueError("A 'review' mező csak true vagy false lehet.")
        self.analyzer.set_diagnostic_review(str(payload["config_key"]), review)
        return {"thresholds": self.thresholds.get_ui_schema()}

    def bulk_issue_action(self, payload: Dict[str, Any]):
        action = str(payload.get("action", "")).lower()
        issue_ids = payload.get("issue_ids") or ([payload["issue_id"]] if payload.get("issue_id") else [])
        if not issue_ids:
            raise ValueError("Legalább egy issue_id szükséges.")

        # A felhasználói művelet nem indít újramérést: a következő mérés a saját szabályai szerint kezeli
        with self.repo.transaction() as db:
            changes = self.analyzer.apply_issue_action(db, issue_ids, action)

        return {
            "action": action,
            "changed": changes,
            "changed_count": len(changes),
            "issues": [self.issue_detail(str(issue_id)) for issue_id in issue_ids],
        }

    def _require_scheduler(self):
        if self.scheduler is None:
            raise RuntimeError("A szimulációs scheduler nincs bekötve.")
        return self.scheduler

    def simulation_status(self):
        return self._require_scheduler().get_status()

    def update_simulation(self, payload: Dict[str, Any]):
        scheduler = self._require_scheduler()
        action = str(payload.get("action", "")).lower()
        if action in {"start", "restart"}:
            duration_seconds = int(payload.get("duration_seconds", 600))
            if duration_seconds not in self.SIMULATION_DURATIONS:
                raise ValueError("Az időtartam csak 600, 3600 vagy 0 másodperc lehet.")
            return scheduler.start(duration_seconds=duration_seconds)
        if action == "source":
            return scheduler.change_source(str(payload.get("source", "")))
        if action == "step_day":
            duration_seconds = int(payload.get("duration_seconds", 600))
            if duration_seconds not in self.SIMULATION_DURATIONS:
                raise ValueError("Az időtartam csak 600, 3600 vagy 0 másodperc lehet.")
            return scheduler.step_day(duration_seconds=duration_seconds)
        if action == "pause":
            return scheduler.pause()
        if action == "resume":
            return scheduler.resume()
        if action == "stop":
            return scheduler.stop()
        if action == "reset":
            result = scheduler.reset()
            if getattr(scheduler, "on_reset", None) is None:
                self.discovery.reset()  # a feltárás állapota is újraindul; a tudástár tényei megmaradnak
            result["reset"] = True
            return result
        raise ValueError("Ismeretlen szimulációs művelet.")


class DashboardHandler(BaseHTTPRequestHandler):
    service: DashboardService
    dashboard_path: Path

    def _send_json(self, status: int, payload: Any):
        try:
            body = json.dumps(
                clean_nan(payload),
                ensure_ascii=False,
                allow_nan=False,
                default=self._json_default,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        except Exception as error:
            self._log_exception(error)
            fallback = json.dumps(
                {"status": "error", "message": str(error)},
                ensure_ascii=False,
            ).encode("utf-8")
            try:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(fallback)))
                self.end_headers()
                self.wfile.write(fallback)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                return

    @staticmethod
    def _json_default(value: Any):
        return clean_nan(value)

    @staticmethod
    def _log_exception(error: Exception):
        print(f"[DASHBOARD API ERROR] {error}", flush=True)
        traceback.print_exc()

    def _send_error(self, status: int, error: Exception):
        if isinstance(error, (ValueError, KeyError)) and status == 500:
            # érvényes, de nem teljesíthető kérés (pl. kevés adat egy döntéshez): 400, rövid naplósor, traceback nélkül
            status = 400
            message = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
            print(f"[DASHBOARD API] {self.path}: {message}", flush=True)
            return self._send_json(status, {"status": "error", "message": message})
        self._log_exception(error)
        return self._send_json(status, {"status": "error", "message": str(error)})

    def _send_file(self, path: Optional[Path] = None):
        body = (path or self.dashboard_path).read_bytes()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._send_file()
        if parsed.path == "/analysis":
            return self._send_file(self.dashboard_path.with_name("analysis.html"))
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/summary":
                return self._send_json(200, self.service.summary())
            if parsed.path == "/api/incidents":
                return self._send_json(200, self.service.incidents(query))
            if parsed.path.startswith("/api/incidents/"):
                value = self.service.incident_detail(parsed.path.rsplit("/", 1)[-1])
            elif parsed.path == "/api/issues":
                return self._send_json(200, self.service.issues(query))
            elif parsed.path.startswith("/api/issues/") and parsed.path.endswith("/lesson"):
                return self._send_json(200, self.service.discovery.lesson_options(parsed.path.split("/")[-2]))
            elif parsed.path.startswith("/api/issues/"):
                value = self.service.issue_detail(parsed.path.rsplit("/", 1)[-1])
            elif parsed.path == "/api/thresholds":
                return self._send_json(200, self.service.get_thresholds())
            elif parsed.path == "/api/thresholds/schema":
                return self._send_json(200, self.service.get_threshold_schema())
            elif parsed.path.startswith("/api/simulation"):
                return self._send_json(200, self.service.simulation_status())
            elif parsed.path == "/api/discovery":
                show_all = query.get("all", [""])[0] in {"1", "true", "yes"}
                return self._send_json(200, self.service.discovery.overview(show_all))
            elif parsed.path.startswith("/api/discovery/candidates/") and parsed.path.endswith("/analysis"):
                value = self.service.discovery.analysis(parsed.path.split("/")[-2])
            elif parsed.path.startswith("/api/discovery/candidates/"):
                value = self.service.discovery.candidate(parsed.path.rsplit("/", 1)[-1])
            elif parsed.path == "/api/discovery/children":
                return self._send_json(200, self.service.discovery.children_overview())
            elif parsed.path == "/api/discovery/backlog":
                days = int(query.get("days", ["28"])[0] or 28)
                return self._send_json(200, self.service.discovery.backlog_calendar(query.get("group", [""])[0], max(7, min(days, 90))))
            elif parsed.path == "/api/discovery/knowledge":
                return self._send_json(200, self.service.discovery.knowledge_overview())
            elif parsed.path == "/api/discovery/review":
                return self._send_json(200, self.service.discovery.review())
            elif parsed.path == "/api/discovery/config":
                return self._send_json(200, self.service.discovery.config_schema())
            else:
                return self._send_error(404, ValueError("Not found"))
            return self._send_json(200 if value is not None else 404, value or {"status": "error", "message": "Not found"})
        except Exception as error:
            return self._send_error(500, error)

    DISCOVERY_ACTIONS = {
        "/api/discovery/run": lambda service, payload: service.run(),
        "/api/discovery/decide": lambda service, payload: service.decide(payload),
        "/api/discovery/unrefine": lambda service, payload: service.unrefine(payload),
        "/api/discovery/suppress": lambda service, payload: service.suppress(payload),
        "/api/discovery/backup": lambda service, payload: service.backup_action(payload),
        "/api/discovery/breakdown": lambda service, payload: service.breakdown_feedback(payload),
        "/api/discovery/generalize": lambda service, payload: service.generalize(payload),
        "/api/issues/lesson": lambda service, payload: service.close_issue_with_lesson(payload),
        "/api/discovery/schedule": lambda service, payload: service.schedule_action(payload),
        "/api/discovery/threshold-override": lambda service, payload: service.threshold_override_action(payload),
        "/api/discovery/children": lambda service, payload: service.children_action(payload),
        "/api/discovery/fact": lambda service, payload: service.fact(payload),
        "/api/discovery/rule": lambda service, payload: service.rule(payload),
        "/api/discovery/availability": lambda service, payload: service.availability(payload),
        "/api/discovery/config": lambda service, payload: service.update_config(payload),
    }

    def do_POST(self):
        if not (self.path.startswith("/api/simulation") or self.path in self.DISCOVERY_ACTIONS or self.path in {
            "/api/thresholds",
            "/api/thresholds/update",
            "/api/issues/bulk-action",
            "/api/diagnostics/toggle",
            "/api/diagnostics/review",
        }):
            return self._send_error(404, ValueError("Not found"))
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body_text = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            payload = json.loads(body_text) if body_text.strip() else {}

            if self.path.startswith("/api/simulation"):
                sub_path = self.path[len("/api/simulation"):].strip("/")
                if sub_path and sub_path not in {"state"}:
                    payload["action"] = payload.get("action", sub_path)
                elif not payload.get("action"):
                    payload["action"] = "start"

                return self._send_json(200, self.service.update_simulation(payload))

            if self.path in self.DISCOVERY_ACTIONS:
                return self._send_json(200, self.DISCOVERY_ACTIONS[self.path](self.service.discovery, payload))
            if self.path == "/api/issues/bulk-action":
                return self._send_json(200, self.service.bulk_issue_action(payload))
            if self.path == "/api/diagnostics/toggle":
                return self._send_json(200, self.service.toggle_diagnostic(payload))
            if self.path == "/api/diagnostics/review":
                return self._send_json(200, self.service.set_review(payload))
            return self._send_json(200, self.service.update_threshold(payload))
        except Exception as error:
            return self._send_error(500, error)

    def log_message(self, format, *args):
        return


class _DashboardHTTPServer(ThreadingHTTPServer):
    """A böngésző induláskor egyszerre több kérést nyit; a lassabb kérések (pl. a backlog-naptár) mellett az
    alapértelmezett 5-ös várakozási sor betelik, és a kapcsolatok eldobódnak ("Failed to fetch")."""

    request_queue_size = 64
    daemon_threads = True


def start_dashboard_server(
    database_path: str = "database.json",
    dashboard_path: str = "dashboard.html",
    host: str = "127.0.0.1",
    port: int = 8765,
    scheduler=None,
    repository=None,
    analyzer=None,
    discovery=None,
):
    service = DashboardService(database_path, scheduler, repository, analyzer, discovery)
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {})
    handler.service = service
    handler.dashboard_path = Path(dashboard_path)
    server = _DashboardHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server