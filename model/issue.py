from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from model.issue_state import IssueState
from model.severity_level import SeverityLevel


if TYPE_CHECKING:
    from model.diagnostics.core.measurement import Measurement

# Az elemző és a tanuló kör által az issue-ra írt adatok: a mérés frissítése nem írja felül őket
KEPT_DETAIL_KEYS = ("lesson", "follow_up", "attached", "attached_news", "known_fault_quiet_since", "known_fault_returned_at")

class Issue:
    def __init__(
        self,
        issue_id: str,
        category: str,
        severity: Any,
        affected_objects: Optional[List[str]] = None,
        affected_incidents: Optional[List[str]] = None,
        history_refs: Optional[List[Dict[str, Any]]] = None,
        description: str = "",
        metric_value: int = 0,
        state: Any = IssueState.ACTIVE,
        groups: Optional[List[str]] = None,
        diagnostic_key: str = "",
        occurrence_key: str = "",
        trigger: str = "",
        window_start: str = "",
        window_end: str = "",
        is_deleted: bool = False,
        is_pinned: bool = False,
        details: Optional[Dict[str, Any]] = None,
        subject: str = "",
        family: str = "",
        affected_type: str = "incident",
        measured_at: str = "",
        first_detected_at: str = "",
        trigger_ref: Optional[Dict[str, Any]] = None,
        window_type: str = "",
        params_snapshot: Optional[Dict[str, Any]] = None,
        params_changed: bool = False,
        # Opcionális paraméterek a régebbi hívásokhoz/migrációhoz
        group_count: Optional[int] = None,
        reassignment_count: Optional[int] = None,
    ):
        self.issue_id = issue_id
        self.category = category
        
        if isinstance(severity, str):
            try:
                self.severity = SeverityLevel(severity)
            except ValueError:
                self.severity = severity
        else:
            self.severity = severity

        # Egységesített objektumlista (kezel incidenseket és issue-kat is)
        combined_aff = set(affected_objects or [])
        if affected_incidents:
            combined_aff.update(affected_incidents)
        self.affected_objects = list(combined_aff)

        self.history_refs = history_refs or []
        self.description = description
        self.metric_value = metric_value
        self.groups = groups or []
        self.diagnostic_key = diagnostic_key or category
        self.occurrence_key = occurrence_key or issue_id
        self.trigger = trigger
        self.window_start = window_start
        self.window_end = window_end
        self.is_deleted = bool(is_deleted)
        self.is_pinned = bool(is_pinned)
        self.updated_at = datetime.now(timezone.utc).isoformat()

        # Mérési metaadatok (a trigger, first_detected_at, trigger_ref epizódonként befagyasztva)
        self.subject = subject
        self.family = family
        self.affected_type = affected_type
        self.measured_at = measured_at
        self.first_detected_at = first_detected_at
        self.trigger_ref = trigger_ref
        self.window_type = window_type
        self.params_snapshot = params_snapshot or {}
        # Figyelmeztetés: a lezárt (NOT_RELEVANT / OUTDATED) mérés óta változott a diagnosztika paramétere
        self.params_changed = bool(params_changed)

        # Rugalmas details tároló a diagnosztika-specifikus adatokhoz
        self.details = details or {}
        if group_count is not None:
            self.details["group_count"] = group_count
        if reassignment_count is not None:
            self.details["reassignment_count"] = reassignment_count

        if isinstance(state, str):
            try:
                self.state = IssueState(state)
            except ValueError:
                self.state = IssueState.ACTIVE
        else:
            self.state = state

    @property
    def affected_incidents(self) -> List[str]:
        """Kompatibilitási property a régi hívásokhoz."""
        return self.affected_objects

    @affected_incidents.setter
    def affected_incidents(self, value: List[str]):
        self.affected_objects = list(value)

    # Kényelmi property-k a Ping-Pong és hasonló diagnosztikákhoz
    @property
    def group_count(self) -> int:
        return self.details.get("group_count", 0)

    @group_count.setter
    def group_count(self, value: int):
        self.details["group_count"] = value

    @property
    def reassignment_count(self) -> int:
        return self.details.get("reassignment_count", 0)

    @reassignment_count.setter
    def reassignment_count(self, value: int):
        self.details["reassignment_count"] = value

    def refresh(self, measurement: "Issue") -> None:
        if self.state in {IssueState.DECLINED, IssueState.ARCHIVED}:
            return
        self.severity = measurement.severity
        
        # Érintett objektumok halmozása duplikációmentesen
        current_affected = set(self.affected_objects)
        if hasattr(measurement, "affected_objects") and measurement.affected_objects:
            current_affected.update(measurement.affected_objects)
        self.affected_objects = list(current_affected)

        self.history_refs = list(measurement.history_refs)
        self.description = measurement.description
        self.metric_value = measurement.metric_value
        self.groups = list(measurement.groups)
        self.diagnostic_key = measurement.diagnostic_key
        self.occurrence_key = measurement.occurrence_key
        self.trigger = measurement.trigger
        self.window_start = measurement.window_start
        self.window_end = measurement.window_end
        self.is_deleted = measurement.is_deleted
        self.is_pinned = self.is_pinned or measurement.is_pinned
        
        # Details összefésülése
        if hasattr(measurement, "details") and measurement.details:
            self.details.update(measurement.details)
            
        self.updated_at = datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_measurement(cls, measurement: "Measurement") -> "Issue":
        """Új issue egy túllépő mérésből; ACTIVE állapotban, új epizóddal."""
        issue = cls(
            issue_id=measurement.issue_id,
            category=measurement.issue_category,
            severity=measurement.severity,
            diagnostic_key=measurement.diagnostic_key,
            occurrence_key=measurement.occurrence_key,
            state=IssueState.ACTIVE,
        )
        issue.apply_measurement(measurement, new_episode=True)
        return issue

    def apply_measurement(self, measurement: "Measurement", new_episode: bool = False) -> None:
        """Mérési eredmény átvétele. Az állapotot nem módosítja; az epizódmezőket csak új epizódnál írja."""
        if measurement.severity is not None:
            self.severity = measurement.severity
        self.category = measurement.issue_category
        self.subject = measurement.subject
        self.family = measurement.family
        self.affected_type = measurement.affected_type
        self.occurrence_key = measurement.occurrence_key
        self.metric_value = measurement.metric_value
        self.description = measurement.description
        # az elemző által az issue-ra írt adatok (tanulság, utókövetés) a mérés frissítése után is megmaradnak;
        # a legutóbbi mérés túllépte-e a küszöböt (lezárt issue-nál a severity nem frissül, ezért külön jelöljük)
        kept = {key: self.details[key] for key in KEPT_DETAIL_KEYS if key in (self.details or {})}
        self.details = {**dict(measurement.details), **kept, "measured_exceeded": measurement.exceeded}
        self.groups = list(self.details.get("groups", self.groups))
        self.affected_objects = list(measurement.affected_objects)
        self.history_refs = list(measurement.history_refs)
        self.measured_at = measurement.measured_at.isoformat() if measurement.measured_at else ""
        window = measurement.window.to_dict()
        self.window_type = window["window_type"]
        self.window_start = window["window_start"]
        self.window_end = window["window_end"]
        self.params_snapshot = deepcopy(dict(measurement.params_snapshot))
        self.params_changed = False
        if new_episode:
            self.trigger = measurement.trigger
            self.first_detected_at = self.measured_at
            self.trigger_ref = measurement.trigger_ref
        self.updated_at = datetime.now(timezone.utc).isoformat()

    def mark_outdated(self) -> None:
        if self.state in {IssueState.ACTIVE, IssueState.NOT_RELEVANT}:
            self.state = IssueState.OUTDATED

    def mark_not_relevant(self) -> None:
        if self.state == IssueState.ACTIVE:
            self.state = IssueState.NOT_RELEVANT

    def resolve(self) -> None:
        if self.state not in {IssueState.DECLINED, IssueState.ARCHIVED, IssueState.DELETED}:
            self.state = IssueState.RESOLVED

    def suppress(self) -> None:
        if self.state not in {IssueState.DECLINED, IssueState.ARCHIVED, IssueState.DELETED}:
            self.state = IssueState.SUPPRESSED

    def activate(self) -> None:
        if self.state != IssueState.DELETED:
            self.state = IssueState.ACTIVE

    def delete(self) -> None:
        self.is_deleted = True
        self.state = IssueState.DELETED

    def decline(self) -> None:
        if self.state in {IssueState.ACTIVE, IssueState.NOT_RELEVANT}:
            self.state = IssueState.DECLINED

    def archive(self) -> None:
        if self.state in {IssueState.NOT_RELEVANT, IssueState.DECLINED}:
            self.state = IssueState.ARCHIVED

    def to_dict(self) -> Dict[str, Any]:
        severity = self.severity.value if hasattr(self.severity, "value") else str(self.severity)
        data = {
            "issue_id": self.issue_id,
            "category": self.category,
            "severity": severity,
            "affected_objects": self.affected_objects,
            "affected_incidents": self.affected_objects,
            "history_refs": self.history_refs,
            "description": self.description,
            "metric_value": self.metric_value,
            "state": self.state.value if hasattr(self.state, "value") else str(self.state),
            "groups": self.groups,
            "diagnostic_key": self.diagnostic_key,
            "occurrence_key": self.occurrence_key,
            "trigger": self.trigger,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "is_deleted": self.is_deleted,
            "is_pinned": self.is_pinned,
            "updated_at": self.updated_at,
            "details": self.details,
            "subject": self.subject,
            "family": self.family,
            "affected_type": self.affected_type,
            "measured_at": self.measured_at,
            "first_detected_at": self.first_detected_at,
            "trigger_ref": self.trigger_ref,
            "window_type": self.window_type,
            "params_snapshot": self.params_snapshot,
            "params_changed": self.params_changed,
        }
        # Kompatibilitásért a details elemeit felszínre is hozhatjuk a dict-ben
        for k, v in self.details.items():
            if k not in data:
                data[k] = v
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Issue":
        severity_raw = data.get("severity", "INFORMATIONAL")
        try:
            severity = SeverityLevel(severity_raw)
        except ValueError:
            severity = severity_raw
            
        aff_objs = data.get("affected_objects") or data.get("affected_incidents", [])
        details = data.get("details", {})
        
        for k in ["group_count", "reassignment_count", "group_chain"]:
            if k in data and k not in details:
                details[k] = data[k]

        issue = cls(
            issue_id=data.get("issue_id", ""),
            category=data.get("category", "UNKNOWN"),
            severity=severity,
            affected_objects=aff_objs,
            history_refs=data.get("history_refs", []),
            description=data.get("description", ""),
            metric_value=data.get("metric_value", 0),
            state=data.get("state", IssueState.ACTIVE.value),
            groups=data.get("groups", []),
            diagnostic_key=data.get("diagnostic_key", ""),
            occurrence_key=data.get("occurrence_key", ""),
            trigger=data.get("trigger", ""),
            window_start=data.get("window_start", ""),
            window_end=data.get("window_end", ""),
            is_deleted=data.get("is_deleted", False),
            is_pinned=data.get("is_pinned", False),
            details=details,
            subject=data.get("subject", ""),
            family=data.get("family", ""),
            affected_type=data.get("affected_type", "incident"),
            measured_at=data.get("measured_at", ""),
            first_detected_at=data.get("first_detected_at", ""),
            trigger_ref=data.get("trigger_ref"),
            window_type=data.get("window_type", ""),
            params_snapshot=data.get("params_snapshot"),
            params_changed=data.get("params_changed", False),
        )
        issue.updated_at = data.get("updated_at", issue.updated_at)
        if "group_chain" in data:
            issue.group_chain = data["group_chain"]
        return issue
