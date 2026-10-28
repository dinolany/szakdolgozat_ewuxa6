"""Általánosítási javaslatok: amit az elemző ismételten ugyanúgy döntött el, azt a gép felajánlja általános
tudásnak (de nem alkalmazza magától – az elemző fogadja el vagy utasítja el).

Források és javaslatok:
- bontás: ugyanazt a mezőt több különböző szülőnél „zsákutcának” jelölte -> a mező soha ne jöjjön bontásban;
  ugyanazt a mezőt többször „jó útnak” jelölte -> a mező legyen kiemelt a mintakeresésben;
- elvetési szabályok: több szabályban ugyanaz a mezőérték szerepel -> általános szabály erre az értékre;
  több szabályban ugyanaz a mező, különböző értékekkel -> a mező szerepe legyen „csak szűkítő” a keresésben.

Az elutasított javaslat csak akkor jön vissza, ha azóta legalább a küszöbnyi új bizonyíték gyűlt mögé.
"""

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional

from models.discovery.knowledge import Rule
from models.discovery.mining import dimension_class

# a tanult (elemzői döntésből lett) elvetési szabályok forrásai; a tudás- és az általánosított szabály nem számít
_DECISION_SOURCES = ("decision:", "cluster")
# Entitás-mezők: hogy több csoport (service, egység…) egy-egy mintáját elvetette, nem jelenti, hogy a mező
# rossz keresési szempont – ezekre nincs mező-szintű („csak szűkítő”) javaslat, csak értékre szóló szabály
ENTITY_DIMENSIONS = frozenset({"assignment_group", "current_group", "business_service", "u_responsibility_unit",
                               "location", "feature", "incident", "date"})


def _is_learned_rejection(rule: Rule) -> bool:
    """A szabály az elemző egy elvetéséből született-e (az általánosítás csak ezekből dolgozik).

    Hívja: generalize.suggestions."""
    return rule.state == "active" and rule.action == "drop" and rule.kind == "rejection" \
        and any(rule.source.startswith(prefix) for prefix in _DECISION_SOURCES)


def suggestions(
    rules: Iterable[Rule],
    breakdown_store: Mapping[str, Any],
    roles: Mapping[str, int],
    dismissed: Mapping[str, int],
    min_count: int = 3,
    labels: Optional[Mapping[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Általánosítási javaslatok: amit az elemző ismételten ugyanúgy döntött el (szabályok, bontási zsákutcák, bevált
    szempontok, bontási utak), azt a gép felajánlja általános tudásnak.

    Hívja: DiscoveryService._generalizations, learning_sim._day."""
    labels = labels or {}
    label = lambda dim: labels.get(dim, dim)
    result: List[Dict[str, Any]] = []

    def offer(key: str, evidence: int, **item: Any) -> None:
        # elutasítás után csak újabb, küszöbnyi bizonyítékkal jön vissza
        if evidence >= min_count and evidence >= dismissed.get(key, -min_count) + min_count:
            result.append({"key": key, "evidence": evidence, **item})

    # --- bontás ---
    prefs = breakdown_store.get("dimensions", {})
    dead_parents: Dict[str, set] = defaultdict(set)
    for entry in breakdown_store.get("log", []):
        if entry.get("verdict") == "dead_end":
            dead_parents[entry["dimension"]].add(entry.get("parent"))
    # bontási út: a szülő mezőfajtája -> a bontó mező (pl. nap -> alkategória), bármely értékkel és csoporttal
    paths = breakdown_store.get("paths", {})
    path_parents: Dict[tuple, set] = defaultdict(set)
    for entry in breakdown_store.get("log", []):
        if entry.get("verdict") == "dead_end":
            for parent_dim in entry.get("parent_dims", []):
                path_parents[(dimension_class(parent_dim), entry["dimension"])].add(entry.get("parent"))
    for (parent_class, child), parents in sorted(path_parents.items()):
        if paths.get(f"{parent_class}>{child}", {}).get("blocked") or prefs.get(child, {}).get("hidden"):
            continue
        offer(f"breakdown_path|{parent_class}>{child}", len(parents), kind="breakdown_path", parent_class=parent_class,
              dimension=child, title=f"Ha a minta {label(parent_class)} szerinti, azon belül ne bontson {label(child)} szerint",
              message=f"{len(parents)} különböző bontásnál, ahol a szülő {label(parent_class)} szerinti volt, zsákutcának "
                      f"jelölted a(z) {label(child)} szerinti bontást. Legyen ez általános út-szabály (bármely "
                      f"{label(parent_class)} és bármely csoport esetén): sem a bontásban, sem a „Bontsd tovább” jelöltjei közt ne jöjjön?")
    for dim, parents in sorted(dead_parents.items()):
        if prefs.get(dim, {}).get("hidden"):
            continue
        offer(f"breakdown_never|{dim}", len(parents), kind="breakdown_never", dimension=dim,
              title=f"„{label(dim)}” soha ne jöjjön bontásban",
              message=f"{len(parents)} különböző bontásnál jelölted zsákutcának. Legyen minden bontásban rejtve?")
    for dim, pref in sorted(prefs.items()):
        if pref.get("good", 0) and roles.get(dim) in (1, 2):
            offer(f"emphasize|{dim}", pref["good"], kind="emphasize", dimension=dim,
                  title=f"„{label(dim)}” legyen kiemelt a mintakeresésben",
                  message=f"{pref['good']}× jelölted jó útnak a bontásokban. Legyen kiemelt szerepű (3), hogy önmagában "
                          f"is gyorsabban jelöltté váljon, és előrébb kerüljön?")

    # --- elvetési szabályok ---
    learned = [rule for rule in rules if _is_learned_rejection(rule)]
    by_item: Dict[tuple, List[Rule]] = defaultdict(list)
    by_dim: Dict[str, set] = defaultdict(set)
    by_dim_rules: Dict[str, List[Rule]] = defaultdict(list)
    general = {tuple(sorted((c["dim"], tuple(c["values"])) for c in rule.conditions))
               for rule in rules if rule.state == "active" and rule.action == "drop" and len(rule.conditions) == 1}
    for rule in learned:
        for condition in rule.conditions:
            for value in condition.get("values", []):
                by_item[(condition["dim"], value)].append(rule)
                by_dim[condition["dim"]].add(value)
                by_dim_rules[condition["dim"]].append(rule)
    for (dim, value), matched in sorted(by_item.items()):
        if len(matched) < 2 or (((dim, (value,)),) in general):
            continue
        measures = sorted({m for rule in matched for m in rule.measure_filter.get("measures", [])}) \
            if all(rule.measure_filter.get("mode") == "list" for rule in matched) else []
        offer(f"general_rule|{dim}={value}", len(matched), kind="general_rule", dimension=dim, value=value, measures=measures,
              rules=[rule.rule_id for rule in matched],
              title=f"Általános szabály: {label(dim)} = {value}",
              message=f"{len(matched)} elvetési szabályodban szerepel ({', '.join(r.rule_id for r in matched)}). "
                      f"Legyen általános: ha a minta tartalmazza, nem jelölt" +
                      (f" a(z) {', '.join(measures)} mérésnél?" if measures else " (minden mérésnél)?"))
    for dim, values in sorted(by_dim.items()):
        if len(values) >= 2 and roles.get(dim) in (2, 3) and dim not in ENTITY_DIMENSIONS and ":" not in dim:
            offer(f"dimension_role|{dim}", len(by_dim_rules[dim]), kind="dimension_role", dimension=dim, role=1,
                  title=f"„{label(dim)}” legyen csak szűkítő mező",
                  message=f"{len(by_dim_rules[dim])} elvetési szabályodban szerepel, {len(values)} különböző értékkel. "
                          f"Legyen csak szűkítő (1): önmagában ne legyen minta, kombinációban csak ha érdemben erősít?")
    result.sort(key=lambda s: -s["evidence"])
    return result
