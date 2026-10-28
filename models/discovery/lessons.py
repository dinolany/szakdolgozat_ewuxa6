"""Lezárás tanulsággal: bármely issue lezárásakor az elemző megmondja, mi derült ki, és az visszajut a tudásba.

Az issue a nyomozás helye (pl. egy torlódásnál az elemző utánajár a csoport munkaidejének). Amit a nyomozás
kiderít, az a lezáráskor tény, szabály, ismert eltérés vagy megerősítés lesz – így a következő hasonló esetnél
a gép már tudja. Tanulságok:

- confirmed: valódi hiba volt -> a findinghoz vezető döntés megerősítést kap (a tipp erősebben tanul belőle);
- off_hours: munkaidőn kívüli várakozás -> nem_dolgozik(csoport, nap) tények;
- not_expected: a funkció ennél a csoportnál nem elvárt -> nem_elvart(funkció, csoport) tény;
- false_alarm: nem hiba -> szabály a jelzés / minta leírására (ilyen ne jöjjön);
- known_deviation: ismert, elfogadott eltérés -> a minta saját normához mérve (csak feltárási mintánál);
- none: lezárás tanulság nélkül.

Ez a modul tiszta: a lehetőségeket és az alapértékeket állítja elő; a végrehajtás a szolgáltatásé.
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence

from models.discovery.mining import with_implied

UNIQUE_DIMENSIONS = {"incident", "subject"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
WEEKDAY_LABELS = {"Mon": "hétfő", "Tue": "kedd", "Wed": "szerda", "Thu": "csütörtök", "Fri": "péntek",
                  "Sat": "szombat", "Sun": "vasárnap"}

OUTCOMES: Dict[str, Dict[str, str]] = {
    "confirmed": {"label": "Valódi hiba volt",
                  "description": "A jelzés helyes volt. A gép megerősítésként tanulja: a hasonló jelölteket erősebben tippeli findingnak."},
    "off_hours": {"label": "Munkaidőn kívüli várakozás",
                  "description": "A csoport ekkor nem dolgozik (pl. hétvégén). Tény lesz belőle: a gép a jövőben ezt a várakozást nem tekinti hibának."},
    "not_expected": {"label": "Ennél a csoportnál a funkció nem elvárt",
                     "description": "Nem edukációs hiány (pl. azonnal zárnak close notes-szal). Tény lesz belőle: a csoportnál ez a funkciójelzés nem jön."},
    "false_alarm": {"label": "Nem hiba – ilyen ne jöjjön",
                    "description": "Téves jelzés volt. Szabály lesz belőle a jelzés leírására; a Tudás fülön visszavonható."},
    "adjust_threshold": {"label": "Ennél (csoportnál, útvonalnál, service-nél) más a küszöb",
                         "description": "A detektor itt túl érzékeny vagy túl engedékeny. Szűkebb hatókörű küszöb lesz belőle: "
                                        "ennél a hatókörnél ez számít, máshol marad a globális (Tudás fülön visszavonható)."},
    "known_deviation": {"label": "Ismert, elfogadott eltérés",
                        "description": "Ez a csoport/minta szokásos viselkedése. A gép a saját normájához méri: csak az ennél rosszabb jelez."},
    "none": {"label": "Lezárás tanulság nélkül", "description": "Az issue lezárul, a tudás nem változik."},
}


def _values(items: Sequence[Sequence[str]], dimension: str) -> List[str]:
    """Egy mező értékei a leírásban (a lezárási űrlap alapértékeihez).

    Hívja: lessons.lesson_options."""
    found: List[str] = []
    for dim, value in with_implied(tuple(sorted(tuple(i) for i in items))):
        if dim == dimension and value not in found:
            found.append(value)
    return found


def lesson_options(issue: Mapping[str, Any], candidate: Optional[Any], signal_items: Sequence[Sequence[str]],
                   diagnostic_key: str, threshold_defs: Sequence[Mapping[str, Any]] = (),
                   entity_items: Sequence[Sequence[str]] = ()) -> Dict[str, Any]:
    """Az issue-hoz illő tanulságok és az előre kitöltött értékek (csoportok, napok, funkció, szabály-leírás).

    Hívja: DiscoveryService.lesson_options."""
    details = issue.get("details") or {}
    items = [list(i) for i in (candidate.items if candidate is not None else signal_items)]
    groups = _values(items, "assignment_group") or [g for g in (issue.get("groups") or []) if g] or \
        ([details["group"]] if details.get("group") and details.get("group") != "*" else [])
    days = _values(items, "weekday")
    feature = next((v for d, v in items if d == "feature"), details.get("feature", ""))
    available = ["confirmed", "off_hours"]
    if feature:
        available.append("not_expected")
    if items:
        available.append("false_alarm")
    if threshold_defs and entity_items:
        available.append("adjust_threshold")
    if candidate is not None and candidate.kind == "contrast":
        available.append("known_deviation")
    available.append("none")
    return {
        "issue_id": issue.get("issue_id"), "diagnostic": diagnostic_key,
        "candidate_id": candidate.candidate_id if candidate is not None else None,
        "outcomes": [{"key": k, **OUTCOMES[k]} for k in available],
        # a szabályba alapból nem kerül egyedi azonosító (egy konkrét jegy): az a jövőben sosem találna
        "defaults": {"groups": groups, "days": days, "feature": feature, "items": items,
                     "rule_items": [i for i in items if i[0] not in UNIQUE_DIMENSIONS],
                     "measure": candidate.measure if candidate is not None else diagnostic_key,
                     "threshold_defs": list(threshold_defs),
                     "entity_items": [list(i) for i in entity_items if i[0] not in UNIQUE_DIMENSIONS]},
        "weekdays": [{"key": d, "label": WEEKDAY_LABELS[d]} for d in WEEKDAYS],
    }


def describe_lesson(outcome: str, created: Mapping[str, List[str]], groups: Sequence[str], days: Sequence[str],
                    feature: str) -> str:
    """A lezárás tanulsága szavakban (mi jött létre: tény, szabály, küszöb, norma).

    Hívja: DiscoveryService.close_issue_with_lesson."""
    label = OUTCOMES.get(outcome, {}).get("label", outcome)
    parts = [label]
    if outcome == "off_hours" and groups and days:
        parts.append(f"{len(groups)} csoport, {', '.join(WEEKDAY_LABELS.get(d, d) for d in days)}")
    if outcome == "not_expected" and feature:
        parts.append(f"{feature} · {len(groups)} csoport")
    made = [f"{len(v)} {k} ({', '.join(v)})" if k == "küszöb" else f"{len(v)} {k}" for k, v in created.items() if v]
    if made:
        parts.append("létrejött: " + ", ".join(made))
    return " – ".join(parts)
