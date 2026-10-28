"""Döntési csomópontok: a jelöltek közös részmintái, amelyekről egyetlen döntés többet is eldönt.

Két fajta kapcsolat:
- hatókör: egy jelölt szűkítései ugyanazon a mérésen (a "G" minta döntése a "G & vasárnap"-ot is érinti:
  elvetésnél a szabály elrejti, findingnál lefedi);
- csomópont: egy részminta (pl. assignment_group=G), amely több jelöltben is szerepel, akkor is, ha maga
  nem jelölt. Egy szabállyal egyben eldönthető.

A "döntésterhelés" azt méri, hány elemzői döntés kell egy jelöltsor feldolgozásához, ha a csomópontokat
egyben döntjük el (mohó lefedés): ez a zaj valódi ára, nem a jelöltek darabszáma.
"""

from collections import defaultdict
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from models.discovery.mining import with_implied

# Csomópont csak entitásra (csoport, service, ...) vagy egy konkrét napra (esemény) épülhet: az olyan
# részminta, mint impact=3 vagy hour=10, szinte minden jelöltben ott van, és egyben senki nem döntene róla
CLUSTER_DIMENSIONS = frozenset({"assignment_group", "business_service", "subcategory", "u_responsibility_unit",
                                "location", "date", "feature"})   # feature: ugyanaz a funkció több csoportnál

Item = Tuple[str, str]
Entry = Tuple[str, str, Sequence[Sequence[Item]]]   # (azonosító, mérés, leírások)


def _items_of(descriptions: Sequence[Sequence[Item]]) -> Set[Item]:
    """A jelölt leírásaiból az entitás-mezők (csoport, service…) elemei: a csomópontok ezekből állnak össze.

    Hívja: clusters.find_clusters."""
    items: Set[Item] = set()
    for description in descriptions:
        items.update(with_implied(tuple(sorted(description))))
    return items


def reach(items: Sequence[Item], measure: str, others: Iterable[Entry], own_id: str = "") -> List[str]:
    """A jelölt szűkítései a többi jelölt közül (ugyanazon a mérésen): ezeket a döntése is érinti.

    Hívja: DiscoveryManager.refresh_suggestions."""
    general = set(items)
    return sorted(
        other_id for other_id, other_measure, descriptions in others
        if other_id != own_id and other_measure == measure
        and any(general < set(with_implied(tuple(sorted(d)))) for d in descriptions)
    )


def find_clusters(entries: Sequence[Entry], min_members: int = 2) -> List[Dict]:
    """Közös részminták legalább `min_members` jelöltben, a legnagyobbtól. Az azonos taghalmazú
    részminták egy csomópontba kerülnek (pl. ha a G csoport minden jelöltje az S service-é, "G" és "S" együtt).

    Hívja: DiscoveryManager.clusters, clusters.decision_load."""
    members_of: Dict[Item, Set[str]] = defaultdict(set)
    measures_of: Dict[str, str] = {}
    for entry_id, measure, descriptions in entries:
        measures_of[entry_id] = measure
        for item in _items_of(descriptions):
            if item[0] in CLUSTER_DIMENSIONS:
                members_of[item].add(entry_id)
    by_members: Dict[frozenset, List[Item]] = defaultdict(list)
    for item, members in members_of.items():
        if len(members) >= min_members:
            by_members[frozenset(members)].append(item)
    clusters = [
        {"items": sorted(items), "members": sorted(members), "measures": sorted({measures_of[m] for m in members})}
        for members, items in by_members.items()
    ]
    clusters.sort(key=lambda c: (-len(c["members"]), len(c["items"]), c["items"]))
    return clusters


def decision_load(entries: Sequence[Entry], min_members: int = 2) -> Dict[str, int]:
    """Hány döntés kell a jelöltekhez, ha a csomópontokat egyben döntjük el (mohó lefedés).

    Hívja: backtest._day_signals."""
    remaining = {entry_id for entry_id, _, _ in entries}
    clusters = find_clusters(entries, min_members)
    decisions = 0
    while remaining:
        best = max(clusters, key=lambda c: len(remaining & set(c["members"])), default=None)
        covered = remaining & set(best["members"]) if best else set()
        if len(covered) < min_members:
            break
        remaining -= covered
        decisions += 1
    return {"candidates": len(entries), "cluster_decisions": decisions, "single_decisions": len(remaining),
            "decisions": decisions + len(remaining)}
