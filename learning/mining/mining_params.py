from dataclasses import dataclass
from typing import FrozenSet, Tuple


@dataclass(frozen=True)
class MiningParams:
    max_order: int = 2              # legfeljebb ennyi dimenzióérték egy mintában
    min_support: int = 3            # legalább ennyi rossz ticket a mintában (csak ezek tesztelődnek)
    min_lift: float = 2.0           # a mintabeli arány legalább ennyiszerese az alaparánynak
    fdr_q: float = 0.10             # elfogadott hamis felfedezési arány (Benjamini–Hochberg)
    min_improvement: float = 1.2    # összetett minta: ennyivel nagyobb lift kell minden részmintájánál
    explained_share: float = 0.8    # általános minta kiesik, ha rossz ticketjeinek ekkora részét egy szűkítése lefedi
    min_distinct_days: int = 1      # ismétlődő időbeli mintánál (hét napja / óra) ennyi különböző nap kell
    top_n: int = 50
    # Dimenziók szerepe: a kizártakat a kereső nem használja; a "csak szűkítő" (pl. impact, alkategória:
    # alapmezők, amelyeket alig állít bárki) önmagában nem minta, és kombinációban is csak akkor marad,
    # ha a nélküle vett mintánál legalább `suppressed_improvement`-szer erősebb
    excluded: FrozenSet[str] = frozenset()
    suppressed: FrozenSet[str] = frozenset()
    suppressed_improvement: float = 1.5
    # Kiemelt dimenziók (pl. business_service): egyelemes mintáik külön FDR-családban, saját szinttel
    emphasized: FrozenSet[str] = frozenset()
    emphasized_fdr_q: float = 0.05
    # tanult zsákutca bontási utak: (szülő mezőfajtája, bontó mező); a kontextusok ilyen gyerekei nem jelöltek
    blocked_paths: FrozenSet[Tuple[str, str]] = frozenset()
