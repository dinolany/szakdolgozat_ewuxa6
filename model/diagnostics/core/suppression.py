"""Elnyomó szabályok a gyári detektorokban.

A tanuló kör szabályai (pl. egy issue „nem hiba” tanulsággal lezárva, vagy egy jelzés elnyomása) a detektor saját,
valós idejű issue-nyitására is hatnak: a túllépő mérésből nem nyílik új issue, ha a leírására (signal_items és az
entitások) illik egy erre a detektorra szóló elvető szabály. A már nyitott issue-k mérése folytatódik.

Egy szabály: {"rule_id", "conditions": [{"dim", "values"}], "measures": [detektor kulcsa, ...]}; a "*" érték a mező
bármely értéke. Egy mezőnek több értéke is lehet (pl. a jegy útjába eső csoportok): bármelyik illeszkedése elég.
"""

from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Set, Tuple


def values_of(items: Iterable[Tuple[str, Any]]) -> Dict[str, Set[str]]:
    """Leírás (mező, érték) párokból mezőnként az értékek halmaza (egy mezőnek több értéke is lehet).
    Hívja: suppressing_rule, és a jelöltek szabályillesztése (models.discovery.knowledge.match_rule)."""
    values: Dict[str, Set[str]] = {}
    for dim, value in items:
        values.setdefault(str(dim), set()).add(str(value))
    return values


def conditions_match(conditions: Sequence[Mapping[str, Any]], values: Mapping[str, Set[str]]) -> bool:
    """Egy szabály feltételei ([{dim, values}]) mind teljesülnek-e: a mező valamelyik értéke a felsoroltak közt van,
    vagy a "*" a mező bármely értékét jelenti. Az egyetlen szabályillesztés a rendszerben: ezt hívja a jelöltekre a
    models.discovery.knowledge.match_rule, a detektorokra a suppressing_rule.

    Hívja: knowledge.match_rule, suppression.suppressing_rule."""
    return all((values.get(c["dim"], set()) & set(c["values"])) or ("*" in c["values"] and c["dim"] in values)
               for c in conditions)


def suppressing_rule(diagnostic_key: str, items: Iterable[Tuple[str, Any]], suppressions: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """Az első, erre a detektorra szóló elvető szabály azonosítója, amelyre a mért eset leírása illik (vagy None).
    Hívja: BaseDiagnosis.run (új issue nyitása előtt, valós időben)."""
    values = values_of(items)
    for rule in suppressions or ():
        if diagnostic_key not in rule.get("measures", ()):
            continue
        conditions = rule.get("conditions") or []
        if conditions and conditions_match(conditions, values):
            return str(rule.get("rule_id", ""))
    return None
