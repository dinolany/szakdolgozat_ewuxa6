from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Mapping


@dataclass(frozen=True)
class ParameterDef:
    """Egy állítható diagnosztikai paraméter (küszöb vagy ablak) leírása."""

    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    step: float
    description: str
    unit: str = ""

    def coerce(self, value: Any) -> float | int:
        """Validálja és a lépésköznek megfelelő típusra alakítja az értéket."""
        numeric = float(value)
        if numeric < self.minimum or numeric > self.maximum:
            raise ValueError(
                f"A(z) {self.key} értéke kívül esik a megengedett tartományon: "
                f"{self.minimum}..{self.maximum}"
            )
        if float(self.step).is_integer() and numeric.is_integer():
            return int(numeric)
        return numeric

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

def resolve_params(definitions: Iterable[ParameterDef], values: Mapping[str, Any] | None) -> Dict[str, Any]:
    """A definíciók defaultjai, felülírva a megadott (ismert kulcsú) értékekkel."""
    resolved = {definition.key: definition.default for definition in definitions}
    for key, value in (values or {}).items():
        if key in resolved:
            resolved[key] = value
    return resolved
