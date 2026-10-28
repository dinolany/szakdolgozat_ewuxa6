"""Szűkebb hatókörű küszöbök a gyári detektorokhoz: a globális küszöb csak alapértelmezés.

Egy mérésnél (diagnosztika × a mért eset entitásai: csoport, útvonal, service…) a küszöb így dől el:
1. kézi: az elemző által megadott, szűkebb hatókörű küszöb (pl. issue lezárásakor: „ennél a csoportnál 5”);
2. tanult: a gép által javasolt és jóváhagyott norma;
3. a diagnosztika globális küszöbe (Thresholdok nézet).
Azonos forráson belül a szűkebb (több feltételű) hatókör nyer. Paraméterenként dől el: egy felülírás csak azokat
a küszöböket írja felül, amelyeket megad.
"""

from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


SOURCE_RANK = {"kézi": 0, "tanult": 1}

@dataclass
class ThresholdOverride:
    override_id: str
    diagnostic: str
    scope: List[List[str]]                  # [[dimenzió, érték], …]: mind illeszkedjen
    params: Dict[str, Any]                  # felülírt küszöbök
    source: str = "kézi"
    note: str = ""
    created_at: str = ""
    state: str = "active"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ThresholdOverride":
        """A szűkebb küszöb a tárolt szótárból.

        Hívja: KnowledgeBase.threshold_overrides, KnowledgeBase.view."""
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def to_dict(self) -> Dict[str, Any]:
        """A szűkebb küszöb tárolható szótárként.

        Hívja: DashboardService._thresholds_with_custom, DiscoveryService.knowledge_overview,
        DiscoveryService.threshold_override_action, KnowledgeBase.add_threshold_override."""
        return asdict(self)

    def matches(self, diagnostic: str, items: Iterable[Tuple[str, str]]) -> bool:
        """A küszöb hatóköre illik-e a mért eset leírására (minden hatóköri elem szerepel benne).

        Hívja: threshold_overrides.resolve."""
        present = {(str(d), str(v)) for d, v in items}
        return self.diagnostic == diagnostic and all((str(d), str(v)) in present for d, v in self.scope)

    def label(self) -> str:
        """A küszöb szavakban (detektor, hatókör, értékek, forrás).

        Hívja: DashboardService._thresholds_with_custom, DiscoveryService.knowledge_overview."""
        scope = " & ".join(f"{d}={v}" for d, v in self.scope) or "mindenhol"
        values = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.diagnostic} · {scope}: {values} ({self.source})"

def resolve(base: Mapping[str, Any], overrides: Sequence[ThresholdOverride], diagnostic: str,
            items: Iterable[Tuple[str, str]]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(a feloldott küszöbök, paraméterenként a forrás: "alapértelmezett" vagy a felülírás azonosítója).

    Hívja: BaseDiagnosis.thresholds_for."""
    items = list(items)
    matching = [o for o in overrides if o.state == "active" and o.matches(diagnostic, items)]
    # a legkisebb prioritással kezdünk, a legnagyobbal végzünk (az írja felül a többit)
    matching.sort(key=lambda o: (SOURCE_RANK.get(o.source, 9), -len(o.scope)), reverse=True)
    params = dict(base)
    sources = {key: "alapértelmezett" for key in base}
    for override in matching:
        for key, value in override.params.items():
            if key in params:
                params[key] = value
                sources[key] = f"{override.override_id} ({override.source})"
    return params, sources

def source_summary(sources: Mapping[str, str]) -> str:
    """Melyik felülírás döntött (a mérés details-ébe).

    Hívja: BaseDiagnosis._to_measurement."""
    custom = sorted({s for s in sources.values() if s != "alapértelmezett"})
    return ", ".join(custom) if custom else "alapértelmezett (globális küszöb)"
