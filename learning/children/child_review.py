from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List

from learning.children.child_def import ChildDef, ROLES


@dataclass(frozen=True)
class ChildReviewParams:
    min_runs: int = 10            # ennyi futás után jelez, ha egyszer sem teljesült
    min_decisions: int = 5        # ennyi döntés kell az egyezés megítéléséhez
    min_agreement: float = 0.5    # ennél ritkább egyezés: a jelzése félrevisz
    min_age_days: int = 14        # ennyi idős, egyszer sem futott almérés jelez

def review_children(children: Iterable[ChildDef], now: datetime, params: ChildReviewParams = ChildReviewParams()) -> List[Dict[str, Any]]:
    """Felülvizsgálandó almérések: sosem futott, sosem teljesül, vagy a döntések rendre mást mondanak.
    A megerősítés óta gyűlt statisztikából számol (az elemző döntése nem jön vissza ugyanazért).

    Hívja: DiscoveryService.review."""
    items = []
    for child in children:
        if child.state != "active":
            continue
        stats = child.stats_since_review()
        since = child.reviewed_at or child.created_at
        age = (now - datetime.fromisoformat(since)).total_seconds() / 86400 if since else 0.0
        if stats["decided"] >= params.min_decisions and stats["agreed"] / stats["decided"] < params.min_agreement:
            reason, evidence = "disagreed", stats["decided"]
            message = (f"teljesülése után {stats['decided']} döntésből csak {stats['agreed']} egyezett a jelzésével "
                       f"({ROLES[child.role]}) – félrevisz?")
        elif stats["runs"] >= params.min_runs and stats["triggered"] == 0:
            reason, evidence = "never_triggered", stats["runs"]
            message = (f"{stats['runs']} futásból egyszer sem teljesült – túl szigorú a küszöb ({child.threshold:.0%}), "
                       f"vagy nem ott fut, ahol kellene?")
        elif stats["runs"] == 0 and age >= params.min_age_days:
            reason, evidence = "never_ran", 0
            message = f"{age:.0f} napja él, és még egyszer sem futott: a kiváltójára ({child.triggers_label}) nem illett jelölt"
        else:
            continue
        items.append({"kind": "child", "object_id": child.child_id, "reason": reason, "message": message,
                      "evidence": evidence, "rendered": f"{child.name}: {child.expression}"})
    return items
