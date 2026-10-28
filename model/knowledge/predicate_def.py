from dataclasses import dataclass
from typing import Any, Mapping, Tuple


@dataclass(frozen=True)
class PredicateDef:
    """Egy tényfajta: argumentumai és az, hogy melyik argumentum melyik dimenzióhoz kötődik."""

    name: str
    args: Tuple[Tuple[str, str], ...]  # (argumentum, dimenzió)
    description: str
    # drop: a tényhez tudásszabály tartozik, amely elveti az illeszkedő jelölteket;
    # classify: a tény csak osztályoz (pl. melyik csoport L1), szabály nem tartozik hozzá
    kind: str = "drop"

    def render(self, args: Mapping[str, Any]) -> str:
        """A tény szövege a predikátum alakjában (pl. nem_dolgozik(G, Sat))."""
        return f"{self.name}({', '.join(str(args.get(arg, '?')) for arg, _ in self.args)})"
