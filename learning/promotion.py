from typing import Any, Dict, Optional

from learning.pattern_finding_diagnosis import PatternFindingDiagnosis
from learning.state.candidate import Candidate
from model.diagnostics.core import MeasurementContext
from model.issue import Issue
from model.issue_state import IssueState


def finding_definition(candidate: Candidate, bad_threshold: float) -> Dict[str, Any]:
    """Az elfogadott minta definíciója: ezt tárolja az issue, és ebből méri újra a diagnosztika.

    Hívja: promotion.promote_finding."""
    return {
        "measure": candidate.measure,
        "pattern": [list(item) for item in candidate.items],
        "alternatives": list(candidate.alternatives),
        "bad_threshold": bad_threshold,
        "candidate_id": candidate.candidate_id,
    }

def promote_finding(
    db_data: Dict[str, Any],
    issue_manager: Any,
    diagnostic: PatternFindingDiagnosis,
    candidate: Candidate,
    ctx: MeasurementContext,
    bad_threshold: float,
) -> Optional[Issue]:
    """Finding -> issue: a minta első mérése a diagnosztika saját ablakán és küszöbein.

    Ha a minta a diagnosztika küszöbei szerint épp nem kiugró (pl. szigorúbbak, mint a feltárásé),
    az issue akkor is létrejön, mert az elemző döntött róla, de NOT_RELEVANT állapotban.
    Ha az ablakban nincs a mintára illő ticket, nem jön létre issue (None).
    

    Hívja: DiscoveryCli.cmd_finding, DiscoveryService.decide."""
    run = diagnostic.seed_run(ctx, finding_definition(candidate, bad_threshold))
    if not run.measurements:
        return None
    measurement = run.measurements[0]
    issue_manager.apply_run(db_data, run, reason="finding_accepted")
    # küszöb alatt az apply_run nem nyit: az elemző döntött róla, ezért NOT_RELEVANT-ként nyílik
    return issue_manager.open_issue(db_data, measurement, IssueState.NOT_RELEVANT, "finding_accepted_below_threshold")
