from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Mapping, Tuple

from model.diagnostics.detectors.feature_usage.occasion_detectors import DETECTORS


@dataclass(frozen=True)
class FeatureDef:
    """Egy vizsgált funkció. A `kind` választja ki az általános felismerőt (ld. DETECTORS), így új
    mező kódírás nélkül, a katalógusfájlban is felvehető."""

    key: str
    label: str
    polarity: int                         # +1: a használat jó jel; -1: a bekövetkezés rossz jel
    fields: Tuple[str, ...]
    kind: str
    applies: str = ""                     # mikor várható el (megjelenítéshez; üresen a felismerő leírása)
    ignore_values: FrozenSet[str] = frozenset()   # alapértékek, amelyek nem számítanak használatnak
    levels: FrozenSet[str] = frozenset()  # csak ezen szintű csoportokra (üres: mindenkire)
    verb: str = ""                        # a használat szavakban, pl. "ír munkajegyzetet" (a jelzés fejlécéhez)

    @property
    def description(self) -> str:
        return self.applies or DETECTORS[self.kind][0]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FeatureDef":
        polarity = data.get("polarity", 1)
        if isinstance(polarity, str):
            polarity = -1 if polarity.strip() in ("-", "−") else 1
        kind = data["kind"]
        if kind not in DETECTORS:
            raise ValueError(f"Ismeretlen felismerő: {kind} (lehetséges: {', '.join(DETECTORS)})")
        return cls(
            str(data["key"]), str(data.get("label", data["key"])), int(polarity), tuple(data["fields"]), kind,
            str(data.get("applies", "")), frozenset(str(v).lower() for v in data.get("ignore_values", ())),
            frozenset(data.get("levels", ())), str(data.get("verb", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "polarity": "+" if self.polarity > 0 else "-",
                "fields": list(self.fields), "kind": self.kind, "applies": self.applies,
                "ignore_values": sorted(self.ignore_values), "levels": sorted(self.levels), "verb": self.verb}
