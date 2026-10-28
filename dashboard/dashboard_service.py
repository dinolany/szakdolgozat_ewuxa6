import json
from pathlib import Path
import threading
from typing import Any, Dict

from engine.analyzer import IncidentAnalyzer
from learning.discovery_service import DiscoveryService
from storage.incident_repository import IncidentRepository
from storage.threshold_store import THRESHOLDS


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
