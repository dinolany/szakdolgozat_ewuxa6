from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import traceback
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from dashboard.dashboard_service import DashboardService
from storage.json_cleaning import clean_nan


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
