"""Közös tudástár: az elemző által rögzített tények a világról, amelyeket a feltárás és minden
diagnosztika ugyanúgy lát (pl. melyik csoport L1, melyik csoport mikor nem dolgozik, melyik
ServiceNow-funkció elérhető kinek).

A tár saját JSON-fájlban él, függetlenül a feltárási állapottól és az incidens-adatbázistól.
A diagnosztikák egy adott pillanatra érvényes, csak olvasható nézetet kapnak (KnowledgeView)
a mérési kontextusban; a tényeket a feltárási konzol (vagy más felület) írja.
"""

from typing import Dict

from model.knowledge.predicate_def import PredicateDef


NEM_DOLGOZIK = PredicateDef(
    "nem_dolgozik",
    (("csoport", "assignment_group"), ("nap", "weekday")),
    "A megoldócsoport az adott napon nem dolgozik (nincs rendelkezésre állás).",
)

SZINT = PredicateDef(
    "szint",
    (("csoport", "assignment_group"), ("szint", "level")),
    "A megoldócsoport támogatási szintje (pl. L1: központi első szint).",
    kind="classify",
)

ELERHETO = PredicateDef(
    "elerheto",
    (("funkcio", "feature"), ("csoport", "assignment_group")),
    "A ServiceNow funkció elérhető a csoportnak (rajta van az űrlapjukon); csoport=* : mindenkinek.",
    kind="classify",
)

NEM_ELERHETO = PredicateDef(
    "nem_elerheto",
    (("funkcio", "feature"), ("csoport", "assignment_group")),
    "A ServiceNow funkció nem elérhető a csoportnak (nincs az űrlapjukon); csoport=* : senkinek.",
    kind="classify",
)

NEM_ELVART = PredicateDef(
    "nem_elvart",
    (("funkcio", "feature"), ("csoport", "assignment_group")),
    "A funkció használata ennél a csoportnál nem elvárt (pl. azonnal zárnak close notes-szal): a hiánya nem edukációs jelzés.",
)

MUNKAIDO = PredicateDef(
    "munkaido",
    (("csoport", "assignment_group"), ("napok", "weekdays"), ("tol", "hour"), ("ig", "hour")),
    "A csoport munkaideje: napok (Mon,Tue,… vagy *), tól–ig egész órában (ig: 1–24). A felvételi és a hold idő "
    "ennél a csoportnál munkaórában számít.",
    kind="classify",
)

PREDICATES: Dict[str, PredicateDef] = {p.name: p for p in (NEM_DOLGOZIK, SZINT, ELERHETO, NEM_ELERHETO, NEM_ELVART, MUNKAIDO)}

L1_LEVEL = "L1"

ALL_GROUPS = "*"

CHILD_DIMENSION_PREFIX = "almeres:"   # az almérésből képzett dimenzió neve: almeres:<azonosító>
