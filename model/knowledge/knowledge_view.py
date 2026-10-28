from dataclasses import dataclass, field
from typing import Any, List, Mapping, Tuple

from model.knowledge.fact import Fact
from model.knowledge.predicates import ELERHETO, NEM_ELERHETO, SZINT


@dataclass(frozen=True)
class KnowledgeView:
    """Az egy pillanatban érvényes tények, csak olvasható formában."""

    facts: Tuple[Fact, ...] = ()
    levels: Mapping[str, str] = field(default_factory=dict)                      # csoport -> szint
    availability: Mapping[Tuple[str, str], bool] = field(default_factory=dict)   # (funkció, csoport) -> elérhető
    # keresési szempontként használt almérések: (dimenzió neve, kifejezés); az adatlapokon igen/nem értékkel
    derived: Tuple[Tuple[str, str], ...] = ()
    # csoport -> munkaidő (model.working_time.Schedule): a felvételi és a hold idő munkaórában
    schedules: Mapping[str, Any] = field(default_factory=dict)
    # a gyári detektorok szűkebb hatókörű küszöbei (model.diagnostics.core.threshold_override.ThresholdOverride)
    threshold_overrides: Tuple[Any, ...] = ()
    # a tanuló kör erre a detektorra szóló elvető szabályai (model.diagnostics.core.suppression)
    suppressions: Tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def of(cls, facts: List[Fact]) -> "KnowledgeView":
        """A nézet a tényekből: csoportszintek és funkció-elérhetőség.

        Hívja: KnowledgeBase.view."""
        levels = {f.args["csoport"]: f.args["szint"] for f in facts if f.predicate == SZINT.name}
        availability = {(f.args["funkcio"], f.args["csoport"]): f.predicate == ELERHETO.name
                        for f in facts if f.predicate in (ELERHETO.name, NEM_ELERHETO.name)}
        return cls(tuple(facts), levels, availability)

    def of_predicate(self, predicate: str) -> List[Fact]:
        """Az adott predikátum tényei.

        Hívja: KnowledgeBase.view."""
        return [fact for fact in self.facts if fact.predicate == predicate]

EMPTY_VIEW = KnowledgeView()
