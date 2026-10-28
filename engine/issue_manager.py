from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Iterable

from model.diagnostics.core import BaseDiagnosis, DiagnosticRun, MeasurementContext, WindowType, parse_timestamp
from model.issue import Issue, IssueState

# Felhasználói döntéssel beállított állapotok: a mérés ezeket nem írja felül
USER_STATES = {
    IssueState.RESOLVED,
    IssueState.SUPPRESSED,
    IssueState.DECLINED,
    IssueState.ARCHIVED,
    IssueState.DELETED,
}
# Ezeknél a mérési adatok sem frissülnek
FROZEN_STATES = {IssueState.DECLINED, IssueState.ARCHIVED, IssueState.DELETED}
# A mérés által vezérelt állapotok
MEASURED_STATES = {IssueState.ACTIVE, IssueState.NOT_RELEVANT}


class IssueManager:
    """Issue-k összevezetése és életciklus-változásainak kezelése."""

    # --- DIAGNOSZTIKÁNKÉNTI ÉLETCIKLUS ---

    def apply_run(self, db_data: Dict[str, Any], run: DiagnosticRun, reason: str) -> list[dict]:
        """Egy diagnosztika futásának átvezetése; kizárólag a saját (diagnostic_key) issue-ihoz nyúl.

        - túllépés: új issue ACTIVE-ként, vagy meglévő újra ACTIVE (NOT_RELEVANT / OUTDATED után új epizóddal)
        - mérve, de küszöb alatt: ACTIVE -> NOT_RELEVANT
        - a futás hatókörében, de nincs a mérésben: időablakos mérésnél OUTDATED, egyébként NOT_RELEVANT
        """
        issues = db_data.setdefault("issues", {})
        history = db_data.setdefault("issue_history", [])
        own = {
            issue_id: self._as_issue(data)
            for issue_id, data in issues.items()
            if self._issue_value(data, "diagnostic_key") == run.diagnostic_key
        }
        changes = []

        def record(issue: Issue, old_state: str | None) -> None:
            issues[issue.issue_id] = issue
            new_state = issue.state.value
            if old_state != new_state:
                change = self._state_change(issue, old_state, new_state, reason)
                change["measured_at"] = issue.measured_at
                history.append(change)
                changes.append(change)

        measured_ids = set()
        for measurement in run.measurements:
            measured_ids.add(measurement.issue_id)
            issue = own.get(measurement.issue_id)
            if issue is None:
                if measurement.exceeded:
                    record(Issue.from_measurement(measurement), None)
                continue
            if issue.state in FROZEN_STATES or issue.is_deleted:
                continue
            old_state = issue.state.value
            if issue.state in USER_STATES:
                issue.apply_measurement(measurement)
                if self._known_fault_returned(issue, measurement):
                    # ismert hiba: a "valódi hiba" tanulsággal lezárt issue elcsendesedett, és most újra túllép -> újranyílik
                    issue.apply_measurement(measurement, new_episode=True)
                    issue.details["known_fault_returned_at"] = issue.measured_at
                    issue.state = IssueState.ACTIVE
                    issues[issue.issue_id] = issue
                    change = self._state_change(issue, old_state, issue.state.value, "ismert hiba visszatért")
                    change["measured_at"] = issue.measured_at
                    history.append(change)
                    changes.append(change)
                    continue
            elif measurement.exceeded:
                issue.apply_measurement(measurement, new_episode=issue.state is not IssueState.ACTIVE)
                issue.state = IssueState.ACTIVE
            else:
                issue.apply_measurement(measurement)
                issue.state = IssueState.NOT_RELEVANT
            record(issue, old_state)

        scope = own.keys() if run.scope is None else run.scope & own.keys()
        for issue_id in scope:
            issue = own[issue_id]
            if issue_id in measured_ids or issue.state not in MEASURED_STATES:
                continue
            old_state = issue.state.value
            target = self._absent_state(issue, run)
            if target is IssueState.OUTDATED:
                issue.mark_outdated()
            elif target is IssueState.NOT_RELEVANT:
                issue.mark_not_relevant()
            record(issue, old_state)
        return changes

    @staticmethod
    def _known_fault_returned(issue: Issue, measurement) -> bool:
        """Ismert hiba-fajta (valós időben, minden mérésnél): a "valódi hiba" tanulsággal lezárt issue, ha a mérése
        egyszer elcsendesedett (küszöb alatt), majd újra túllép. Csillapítás: egy csendes mérés kell a visszatérés előtt."""
        if issue.state is not IssueState.RESOLVED or ((issue.details or {}).get("lesson") or {}).get("outcome") != "confirmed":
            return False
        if not measurement.exceeded:
            issue.details.setdefault("known_fault_quiet_since", issue.measured_at)
            return False
        if issue.details.get("known_fault_quiet_since"):
            issue.details.pop("known_fault_quiet_since", None)
            return True
        return False

    @staticmethod
    def _absent_state(issue: Issue, run: DiagnosticRun) -> IssueState | None:
        """Mi legyen a futás hatókörébe tartozó, de a mérésből hiányzó issue sorsa."""
        if run.window_type is WindowType.SLIDING:
            return IssueState.OUTDATED
        if run.window_type is WindowType.TUMBLING:
            if run.scope is not None:
                return IssueState.OUTDATED  # célzott újramérés: kiesett a saját (átméretezett) vödréből
            issue_end = parse_timestamp(issue.window_end)
            if issue_end is not None and run.window.start is not None and issue_end <= run.window.start:
                return IssueState.OUTDATED  # egy korábbi, már lezárt vödör issue-ja
            return None
        return IssueState.NOT_RELEVANT

    def apply_param_change(
        self,
        db_data: Dict[str, Any],
        diagnostic: BaseDiagnosis,
        ctx: MeasurementContext,
        reason: str,
    ) -> list[dict]:
        """Küszöb- vagy ablakparaméter változása után egyetlen diagnosztika issue-inak frissítése.

        - ablak nélküli / SNAPSHOT mérés: teljes újramérés, bármely mérés vezérelte állapot változhat
        - időablakos mérés: csak az ACTIVE issue-k mérődnek újra (a saját ablakvégükhöz horgonyozva,
          az új hosszra igazított ablakban); a NOT_RELEVANT és OUTDATED issue-k nem változnak,
          csak params_changed figyelmeztetést kapnak
        """
        if not diagnostic.window_type.is_timed:
            return self.apply_run(db_data, diagnostic.run(ctx), reason)

        issues = db_data.setdefault("issues", {})
        current_params = diagnostic.current_params(ctx)
        changes = []
        for issue_id, data in list(issues.items()):
            if self._issue_value(data, "diagnostic_key") != diagnostic.key:
                continue
            issue = self._as_issue(data)
            if issue.state is IssueState.ACTIVE:
                changes.extend(self.apply_run(db_data, diagnostic.remeasure(issue, ctx), reason))
            elif issue.state in {IssueState.NOT_RELEVANT, IssueState.OUTDATED}:
                if issue.params_snapshot and issue.params_snapshot != current_params and not issue.params_changed:
                    issue.params_changed = True
                    issues[issue_id] = issue
        # a mostani ablak teljes mérése az új küszöbbel: ami most lépi át (pl. lazább küszöbnél), az azonnal megjelenik
        changes.extend(self.apply_run(db_data, diagnostic.run(ctx), reason))
        return changes

    def apply_action(
        self,
        db_data: Dict[str, Any],
        issue_ids: Iterable[str],
        action: str,
        reason: str = "dashboard_action",
    ) -> list[dict]:
        issues = db_data.setdefault("issues", {})
        history = db_data.setdefault("issue_history", [])
        changes = []
        for issue_id in issue_ids:
            data = issues.get(str(issue_id))
            if data is None:
                continue
            issue = self._as_issue(data)
            old_state = issue.state.value if isinstance(issue.state, Enum) else str(issue.state)
            old_deleted = issue.is_deleted
            old_pinned = issue.is_pinned

            if action == "delete":
                issue.delete()
            elif action == "pin":
                issue.is_pinned = True
            elif action == "unpin":
                issue.is_pinned = False
            elif action == "suppress":
                issue.suppress()
            elif action == "activate":
                issue.activate()
                issue.is_deleted = False
            elif action == "resolve":
                issue.resolve()
            else:
                raise ValueError(f"Ismeretlen issue művelet: {action}")

            issues[str(issue_id)] = issue
            new_state = issue.state.value if isinstance(issue.state, Enum) else str(issue.state)
            if old_state != new_state or old_deleted != issue.is_deleted or old_pinned != issue.is_pinned:
                change = self._state_change(issue, old_state, new_state, reason)
                history.append(change)
                changes.append(change)
        return changes

    def open_issue(self, db_data: Dict[str, Any], measurement: Any, state: IssueState, reason: str) -> Issue:
        """Issue nyitása egy mérésből a megadott állapotban, akkor is, ha a mérés nem lépte át a küszöböt
        (az elemző döntött róla). Meglévő issue-hoz nem nyúl.

        Hívja: promotion.promote_finding."""
        issues = db_data.setdefault("issues", {})
        existing = issues.get(measurement.issue_id)
        if existing is not None:
            return self._as_issue(existing)
        issue = Issue.from_measurement(measurement)
        issue.state = state
        issues[issue.issue_id] = issue
        change = self._state_change(issue, None, issue.state.value, reason)
        change["measured_at"] = issue.measured_at
        db_data.setdefault("issue_history", []).append(change)
        return issue

    def update_details(
        self,
        db_data: Dict[str, Any],
        issue_id: str,
        values: Dict[str, Any] | None = None,
        remove: Iterable[str] = (),
    ) -> Issue | None:
        """A tanuló kör adatainak írása az issue details mezőjébe (tanulság, utókövetés, hozzácsapott jelöltek,
        ismert hiba); állapotot nem vált. A mérés ezeket a kulcsokat megtartja (KEPT_DETAIL_KEYS).

        Hívja: DiscoveryService._attach_to_open_issues, DiscoveryService._check_known_faults,
        DiscoveryService._check_follow_ups, DiscoveryService.close_issue_with_lesson."""
        issues = db_data.setdefault("issues", {})
        data = issues.get(str(issue_id))
        if data is None:
            return None
        issue = self._as_issue(data)
        for key in remove:
            issue.details.pop(key, None)
        issue.details.update(values or {})
        issues[str(issue_id)] = issue
        return issue

    @staticmethod
    def _as_issue(value: Any) -> Issue:
        return value if isinstance(value, Issue) else Issue.from_dict(value)

    @staticmethod
    def _issue_value(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, Issue):
            return getattr(value, key, default)
        return value.get(key, default)

    @staticmethod
    def _state_change(
        issue: Issue, old_state: str | None, new_state: str, reason: str
    ) -> Dict[str, Any]:
        return {
            "issue_id": issue.issue_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "old_state": old_state,
            "new_state": new_state,
            "reason": reason,
            "metric_value": issue.metric_value,
        }