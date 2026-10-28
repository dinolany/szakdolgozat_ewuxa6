"""Issue-trend: sok aktív issue egy kategóriában (a ticket-diagnosztikák után, azok issue-in fut)."""

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from model.diagnostics.core import CrossIssueDiagnosis, MeasurementContext, MeasurementWindow, Observation, ParameterDef
from model.issue import Issue
from model.issue_state import IssueState
from model.severity_level import SeverityLevel


class IssueTrendDiagnosis(CrossIssueDiagnosis):
    key = "ISSUE_TREND"
    config_key = "issue_trend"
    issue_category = "ISSUE_ANALYTICS"
    metric_key = "count"
    metric_label = "aktív issue"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("moderate_count", "Mérsékelt issue darabszám", 5, 1, 100000, 1,
                         "Ennyi aktív issue egy kategóriában aktív trend.", "db"),
            ParameterDef("extreme_count", "Extrém issue darabszám", 15, 1, 100000, 1, "Extrém súlyossági szint küszöbe.", "db"),
        ]

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        count = details.get("count", 0)
        if count >= thresholds["extreme_count"]:
            return SeverityLevel.EXCEEDED_EXTREME
        if count >= thresholds["moderate_count"]:
            return SeverityLevel.EXCEEDED_MODERATE
        return None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        return f"A(z) {subject} kategóriában {details.get('count', 0)} aktív issue van."

    def observe_issues(self, issues: List[Issue], window: MeasurementWindow, ctx: MeasurementContext) -> Iterable[Observation]:
        by_category: Dict[str, List[Issue]] = defaultdict(list)
        for issue in issues:
            if issue.state == IssueState.ACTIVE:
                by_category[issue.category or issue.diagnostic_key].append(issue)
        return [
            Observation(subject=category, details={"count": len(members), "diagnostics": sorted({i.diagnostic_key for i in members})},
                        affected=tuple(sorted(i.issue_id for i in members)))
            for category, members in sorted(by_category.items())
        ]
