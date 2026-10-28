from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratorParams:
    weeks: int = 12                 # ennyi szintetikus hét a valódi idősor elé
    seed: int = 1
    swap_share: float = 0.5         # ekkora eséllyel kap a ticket egy másik (azonos csoportú) lefutást
    open_jitter_minutes: int = 30   # a nyitási idő szórása a sablonéhoz képest (a napon belül marad)
    volume_scale: float = 1.0       # az érkezési ütem szorzója
    observed_days: int = 14         # a sablonok ennyi napról származnak (a napi átlaghoz)
    id_prefix: str = "SYN"
