from typing import Any, Dict, List, Set

from model.history_line import HistoryLine


class Incident:
    # A pontos ServiceNow 'field' értékek teljes körű leképezése a belső FŐ attribútumokra
    FIELD_MAPPING = {
        # Állapot / Státusz
        "state": "state",
        "incident_state": "state",
        "task_effective_state": "state",
        
        # Csoport és Felelős
        "assignment_group": "assignment_group",
        "u_assignment_group": "assignment_group",
        "assigned_to": "assigned_to",
        
        # Prioritás és Súlyosság
        "priority": "priority",
        "severity": "severity",
        "urgency": "urgency",
        "impact": "impact",
        
        # Üzleti szolgáltatás (service)
        "service": "business_service",
        "business_service": "business_service",
        "u_business_service": "business_service",
        
        # Dátumok
        "opened_at": "opened_at",
        "closed_at": "closed_at",
        "resolved_at": "resolved_at",
        "sys_created_on": "opened_at",
        
        # Egyéb fontos törzsadatok
        "category": "category",
        "subcategory": "subcategory",
        "short_description": "short_description",
        "description": "short_description",
        "close_code": "close_code",
    }

    def __init__(self, incident_id: str, raw_data: Dict[str, Any] = None):
        self.id: str = incident_id

        # FŐ ATTRIBÚTUMOK alapértelmezett értékei
        self.state: str = "Ismeretlen (Új)"
        self.assignment_group: str = "Nincs kiosztva"
        self.assigned_to: str = "Nincs kiosztva"
        self.priority: str = "N/A"
        self.severity: str = "N/A"
        self.urgency: str = "N/A"
        self.impact: str = "N/A"
        self.business_service: str = "N/A"
        self.opened_at: str = "N/A"
        self.closed_at: str = "N/A"
        self.resolved_at: str = "N/A"
        self.category: str = "N/A"
        self.subcategory: str = "N/A"
        self.short_description: str = "N/A"
        self.close_code: str = "N/A"

        # Diagnosztikai attribútumok a ping-pong méréshez
        self.ping_pong_count: int = 0
        self.distinct_groups: List[str] = []

        self.raw_data: Dict[str, Any] = raw_data or {}
        self.history: List[HistoryLine] = []
        # A már feldolgozott események azonosítói: ugyanaz a logsor kétszer nem kerül be
        self._history_ids: Set[str] = set()

    def set_ping_pong_data(self, count: int, groups: List[str]):
        """Beállítja a ping-pong mérés eredményeit közvetlenül az incidensen."""
        self.ping_pong_count = count
        self.distinct_groups = groups

    def apply_event(self, history_line: HistoryLine | Dict[str, Any]) -> Dict[str, Any]:
        """Esemény feldolgozása a HistoryLine objektumra támaszkodva.

        Idempotens: a már ismert history_id-jú eseményt kihagyja (pl. újraindított szimuláció,
        vagy élesben az API kétszer küldi ugyanazt a logsort).
        """
        if isinstance(history_line, dict):
            history_line = HistoryLine.from_dict(history_line)

        history_id = history_line.history_id
        if history_id in self._history_ids:
            return {
                "is_main_attribute": False,
                "field": history_line.field,
                "mapped_to": None,
                "before": "N/A",
                "after": "N/A",
                "duplicate": True,
            }
        self._history_ids.add(history_id)
        self.history.append(history_line)

        raw_field = history_line.field
        field_name = str(raw_field).strip().lower().replace(" ", "_")
        if "." in field_name:
            field_name = field_name.split(".")[-1]

        new_val = history_line.new_value
        mapped_attribute = self.FIELD_MAPPING.get(field_name)

        before_summary = "N/A"
        is_main = False

        if mapped_attribute:
            is_main = True
            before_summary = str(getattr(self, mapped_attribute, "N/A"))

            setattr(self, mapped_attribute, new_val)
            self.raw_data[mapped_attribute] = new_val

            # Különleges dátum/állapot logika zárt/megoldott státuszokhoz
            if mapped_attribute in ["state", "incident_state"]:
                if (
                    str(new_val).lower() in ["6", "7", "closed", "resolved", "zárt"]
                    and self.closed_at == "N/A"
                ):
                    ts = history_line.timestamp or "N/A"
                    self.closed_at = str(ts)
            elif mapped_attribute == "resolved_at" and new_val:
                if self.resolved_at == "N/A":
                    self.resolved_at = str(new_val)
            elif mapped_attribute == "closed_at" and new_val:
                if self.closed_at == "N/A":
                    self.closed_at = str(new_val)
        else:
            if field_name:
                self.raw_data[field_name] = new_val

        after_summary = (
            str(getattr(self, mapped_attribute))
            if mapped_attribute
            else str(new_val)
        )

        return {
            "is_main_attribute": is_main,
            "field": field_name,
            "mapped_to": mapped_attribute,
            "before": before_summary,
            "after": after_summary,
            "duplicate": False,
        }

    def to_dict(self) -> Dict[str, Any]:
        """JSON kimenet előállítása a Dashboardhoz és a mentéshez."""
        formatted_history = [h.to_dict() for h in self.history]

        main_state_data = {
            "State (Állapot)": self.state,
            "Assignment Group": self.assignment_group,
            "Assigned To": self.assigned_to,
            "Priority": self.priority,
            "Severity": self.severity,
            "Urgency": self.urgency,
            "Impact": self.impact,
            "Business Service": self.business_service,
            "Category": self.category,
            "Subcategory": self.subcategory,
            "Short Description": self.short_description,
            "Opened At": self.opened_at,
            "Closed At": self.closed_at,
            "Resolved At": self.resolved_at,
            "Close Code": self.close_code,
        }

        return {
            "incident_id": self.id,
            "state": self.state,
            "assignment_group": self.assignment_group,
            "assigned_to": self.assigned_to,
            "priority": self.priority,
            "severity": self.severity,
            "urgency": self.urgency,
            "impact": self.impact,
            "business_service": self.business_service,
            "category": self.category,
            "subcategory": self.subcategory,
            "short_description": self.short_description,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "resolved_at": self.resolved_at,
            "close_code": self.close_code,
            "main_state": main_state_data,
            "current_state": main_state_data,
            "ping_pong_count": self.ping_pong_count,
            "distinct_groups": self.distinct_groups,
            "history_count": len(self.history),
            "history_lines": formatted_history,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Incident":
        """Re-hidratálja az Incident objektumot a database.json szótárából."""
        inc_id = data.get("incident_id", "")
        instance = cls(incident_id=inc_id)

        main_state = data.get("main_state") or data.get("current_state") or {}

        instance.state = data.get("state") or main_state.get("State (Állapot)", instance.state)
        instance.assignment_group = data.get("assignment_group") or main_state.get("Assignment Group", instance.assignment_group)
        instance.assigned_to = data.get("assigned_to") or main_state.get("Assigned To", instance.assigned_to)
        instance.priority = data.get("priority") or main_state.get("Priority", instance.priority)
        instance.severity = data.get("severity") or main_state.get("Severity", instance.severity)
        instance.urgency = data.get("urgency") or main_state.get("Urgency", instance.urgency)
        instance.impact = data.get("impact") or main_state.get("Impact", instance.impact)
        instance.business_service = data.get("business_service") or main_state.get("Business Service", instance.business_service)
        instance.category = data.get("category") or main_state.get("Category", instance.category)
        instance.subcategory = data.get("subcategory") or main_state.get("Subcategory", instance.subcategory)
        instance.short_description = data.get("short_description") or main_state.get("Short Description", instance.short_description)
        instance.opened_at = data.get("opened_at") or main_state.get("Opened At", instance.opened_at)
        instance.closed_at = data.get("closed_at") or main_state.get("Closed At", instance.closed_at)
        instance.resolved_at = data.get("resolved_at") or main_state.get("Resolved At", instance.resolved_at)
        instance.close_code = data.get("close_code") or main_state.get("Close Code", instance.close_code)

        instance.ping_pong_count = data.get("ping_pong_count", 0)
        instance.distinct_groups = data.get("distinct_groups", [])

        history_lines_data = data.get("history_lines", []) or data.get("history", [])
        for h_dict in history_lines_data:
            if isinstance(h_dict, dict):
                instance.apply_event(h_dict)

        return instance