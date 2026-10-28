"""Kontrasztminta-keresés (subgroup discovery) a ticket-adatlapokon.

Egy mérésre (pl. lassú felvétel) megkeresi azokat a dimenzióérték-kombinációkat
(mintákat), ahol a rossz ticketek aránya szignifikánsan és érdemben magasabb az
alaparánynál. Lépései:

1. számlálás: minden legfeljebb `max_order` elemű kombinációra a csoportméret és a rossz ticketek száma
2. tesztelés: egyoldali binomiális próba az alaparányhoz képest; a többszörös tesztelést
   Benjamini–Hochberg (FDR) korrekció kezeli, a méretük alapján tesztelhető minták teljes családján
3. redundanciaszűrés: egy összetett minta csak akkor marad, ha érdemben erősebb minden
   általánosításánál (a dátumos minta a hét napjára általánosítottnál is); egy általános minta
   pedig kiesik, ha a rossz ticketjeit nagyrészt egy megmaradt, erősebb szűkítése magyarázza
"""

import itertools
import math
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from learning.measures import MeasureDef
from learning.profile import DISPLAY_ONLY_DIMENSIONS, TicketProfile, dimension_items

Item = Tuple[str, str]
Pattern = Tuple[Item, ...]

# Egy mintában egymást kizáró dimenziók (a dátum meghatározza a hét napját)
EXCLUSIVE_DIMENSIONS: Tuple[FrozenSet[str], ...] = (frozenset({"date", "weekday"}),)
# Ismétlődést állító időbeli dimenziók (a dátumos minta egyszeri, ezért nem ilyen)
RECURRING_DIMENSIONS = frozenset({"weekday", "hour"})


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


@dataclass(frozen=True)
class PatternResult:
    measure: str
    items: Pattern
    bad_count: int           # rossz egységek (ticketek vagy szakaszok) száma
    group_size: int
    population: int
    base_rate: float
    rate: float
    lift: float
    expected: float          # várható rossz ticketszám alaparány mellett
    excess: float            # többlet rossz ticket a várthoz képest
    p_value: float
    q_value: float           # FDR-korrigált p-érték
    distinct_days: int       # hány különböző napon fordult elő rossz ticket a mintában
    bad_incident_ids: Tuple[str, ...]
    # Más minták, amelyek pontosan ugyanezt a rossz ticket-halmazt írják le (összevonva)
    alternatives: Tuple[Pattern, ...] = ()
    # contrast: a többi egységhez képest kiugró (mine); trend: a saját múltjához képest romlott (trend.py)
    kind: str = "contrast"
    extra: Optional[Dict[str, Any]] = None   # fajtától függő többletadat (pl. a múltbeli arány, a kontextus)

    @property
    def key(self) -> str:
        """A minta kulcsa (mező=érték&…).

        Hívja: PatternResult.candidate_key (átadva)."""
        return pattern_key(self.items)

    @property
    def candidate_key(self) -> str:
        """A jelölt azonosításának kulcsa: ugyanaz a minta más fajtaként külön jelölt."""
        return self.key if self.kind == "contrast" else f"{self.kind}|{self.key}"

    @property
    def alternative_keys(self) -> Tuple[str, ...]:
        """Az ugyanazt az esethalmazt leíró további minták kulcsai."""
        return tuple(pattern_key(items) for items in self.alternatives)

    @property
    def dimensions(self) -> Set[str]:
        """A minta mezői."""
        return {dimension for dimension, _ in self.items}


# Bontási út: a szülő mezőjének FAJTÁJA -> a bontó mező. A fajta általánosít: a nap (hét napja vagy dátum)
# egy fajta, a csoport-mezők egy fajta; így egy tanult út („napon belül ne alkategória szerint”) bármely
# napra és bármely csoportra érvényes.
DIMENSION_CLASSES = {"weekday": "nap", "date": "nap", "hour": "óra", "assignment_group": "csoport", "current_group": "csoport"}


def dimension_class(dimension: str) -> str:
    """A mező fajtája a bontási utakhoz (pl. a hét napja és a dátum „nap”, a csoportmezők „csoport”).

    Hívja: generalize.suggestions, mining.path_blocked."""
    return DIMENSION_CLASSES.get(dimension, dimension)


def path_blocked(parent_dimensions: Iterable[str], child_dimension: str, blocked: Iterable[Tuple[str, str]]) -> bool:
    """Tiltott-e a bontás: a szülő bármely mezőjének fajtájából a bontó mezőbe vezető út tanult zsákutca.

    Hívja: DiscoveryService._filtered_breakdown, DiscoveryService._refinements, mining.mine."""
    blocked = set(blocked)
    child = dimension_class(child_dimension)
    return any((dimension_class(d), child) in blocked or (dimension_class(d), child_dimension) in blocked
               for d in parent_dimensions)


def pattern_key(items: Iterable[Item]) -> str:
    """A minta kanonikus kulcsa: rendezett 'dimenzió=érték' párok '&'-tel.

    Hívja: DiscoveryManager.ingest_signals, DiscoveryService._refinements, DiscoveryService.knowledge_overview,
    PatternFindingDiagnosis.observe_definition, PatternResult.alternative_keys, PatternResult.key,
    knowledge.match_rule, mining.mine, pattern_finding.finding_subject."""
    return "&".join(f"{dimension}={value}" for dimension, value in sorted(items))


def binomial_sf(k: int, n: int, p: float) -> float:
    """P(X >= k), X ~ Binomiális(n, p); log-térben számolva a numerikus stabilitásért.

    Hívja: baseline.compare_to_baseline, mining.mine, pattern_finding.measure_pattern, trend.mine_trends."""
    if k <= 0:
        return 1.0
    if k > n or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    log_p, log_q = math.log(p), math.log1p(-p)
    log_terms = [
        math.lgamma(n + 1) - math.lgamma(j + 1) - math.lgamma(n - j + 1) + j * log_p + (n - j) * log_q
        for j in range(k, n + 1)
    ]
    peak = max(log_terms)
    return min(1.0, math.exp(peak) * sum(math.exp(term - peak) for term in log_terms))


def benjamini_hochberg(p_values: Sequence[float]) -> List[float]:
    """Benjamini–Hochberg szerint korrigált q-értékek, az eredeti sorrendben.

    Hívja: mining.mine, trend.mine_trends."""
    m = len(p_values)
    order = sorted(range(m), key=lambda index: p_values[index])
    q_values = [1.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        index = order[rank - 1]
        running_min = min(running_min, p_values[index] * m / rank)
        q_values[index] = running_min
    return q_values


def _valid_combination(items: Pattern) -> bool:
    """Egy mintaelem-kombináció értelmes-e (pl. nem ugyanaz a mező kétszer, nem a dátum és a hét napja együtt).

    Hívja: mining.mine."""
    dimensions = [dimension for dimension, _ in items]
    if len(set(dimensions)) < len(dimensions):
        return False
    return not any(len(group.intersection(dimensions)) > 1 for group in EXCLUSIVE_DIMENSIONS)


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def with_implied(items: Pattern) -> Pattern:
    """A minta a belőle következő elemekkel bővítve (a dátum meghatározza a hét napját).

    Hívja: DiscoveryCli.cmd_reject, DiscoveryManager._covering_finding, DiscoveryService._run_probes,
    analysis.analyze_candidate, backlog.candidate_groups, children.trigger_matches, clusters._items_of,
    clusters.reach, knowledge.match_rule és mások."""
    values = dict(items)
    if "date" in values and "weekday" not in values:
        try:
            weekday = _WEEKDAYS[datetime.fromisoformat(values["date"]).weekday()]
        except ValueError:
            return items
        return tuple(sorted(items + (("weekday", weekday),)))
    return items


def _generalizations(items: Pattern) -> Iterable[Pattern]:
    """A minta valódi általánosításai, a következményelemeket is figyelembe véve
    (pl. a 'G & 2026-08-09' általánosítása a 'G & vasárnap' is).

    Hívja: mining.mine."""
    expanded = with_implied(items)
    for order in range(1, len(expanded) + 1):
        for sub in itertools.combinations(expanded, order):
            if sub != items:
                yield sub


def mine(
    profiles: Sequence[TicketProfile],
    measure: MeasureDef,
    params: MiningParams = MiningParams(),
    bad_threshold: Optional[float] = None,
    contexts: Sequence[Pattern] = (),
) -> List[PatternResult]:
    """A mérés szempontjából kiemelkedő minták, erősség szerint rendezve.

    `contexts`: tovább bontott minták (pl. a szombat). Ezek szűkítései nem az átlaghoz, hanem a
    kontextus arányához mérve számítanak kiugrónak: a „szombat & X csoport” csak akkor jelez, ha
    a szombatokon belül is kilóg (a szombati lassulás önmagában már nem hír).
    

    Hívja: runner._mine_all."""
    population = [p for p in profiles if p.measures.get(measure.key) is not None]
    # A számlálás egysége a ticket vagy a szakasz (p.unit); a rossz ticketek listája incidensazonosító
    bad_flags = {p.unit: measure.is_bad(p.measures[measure.key], bad_threshold) for p in population}
    total, total_bad = len(population), sum(bad_flags.values())
    if total == 0 or total_bad == 0:
        return []
    base_rate = total_bad / total

    # 1. Számlálás
    group_size: Dict[Pattern, int] = defaultdict(int)
    bad_ids: Dict[Pattern, List[str]] = defaultdict(list)
    bad_days: Dict[Pattern, Set[str]] = defaultdict(set)
    context_sets = list(dict.fromkeys(frozenset(tuple(item) for item in context) for context in contexts if context))
    context_counts = [[0, 0] for _ in context_sets]
    for profile in population:
        items = [item for item in dimension_items(profile.dimensions)
                 if item[0] not in params.excluded and item[0] not in DISPLAY_ONLY_DIMENSIONS]
        is_bad = bad_flags[profile.unit]
        present = set(with_implied(tuple(dimension_items(profile.dimensions))))
        for k, context in enumerate(context_sets):
            if context <= present:
                context_counts[k][0] += 1
                context_counts[k][1] += is_bad
        for order in range(1, params.max_order + 1):
            for combination in itertools.combinations(items, order):
                if not _valid_combination(combination):
                    continue
                group_size[combination] += 1
                if is_bad:
                    bad_ids[combination].append(profile.incident_id)
                    bad_days[combination].add(profile.dimensions.get("date", ""))

    def lift_of(pattern: Pattern) -> float:
        size = group_size.get(pattern, 0)
        return (len(bad_ids.get(pattern, ())) / size) / base_rate if size else 0.0

    context_rates = [(context, bad / size) for context, (size, bad) in zip(context_sets, context_counts) if size]

    def reference(pattern: Pattern) -> Tuple[float, Optional[FrozenSet[Item]]]:
        """Az alaparány, amelyhez a mintát mérjük: a legszigorúbb kontextusé, amelynek szűkítése; különben az átlag."""
        own, implied = frozenset(pattern), set(with_implied(pattern))
        best: Tuple[float, Optional[FrozenSet[Item]]] = (base_rate, None)
        for context, rate in context_rates:
            if context != own and context <= implied and rate > best[0]:
                best = (rate, context)
        return best

    # 2. Tesztelés és FDR-korrekció
    # A tesztcsalád minden minta, amely a mérete alapján elérhetné a min_support-ot. A kiválasztás így
    # nem függ az eredménytől (a rossz ticketek számától), ami a korrekció érvényességéhez szükséges.
    # A min_support alatti minták p-értéke 1 (konzervatív: a korrekciót csak szigorítja).
    tested = [pattern for pattern, size in group_size.items() if size >= params.min_support]
    references = {pattern: reference(pattern) for pattern in tested} if context_rates else {}
    p_values = [
        binomial_sf(len(bad_ids[pattern]), group_size[pattern], references.get(pattern, (base_rate, None))[0])
        if len(bad_ids.get(pattern, ())) >= params.min_support else 1.0
        for pattern in tested
    ]
    # Külön tesztcsaládok, mert az elemző előre megmondta, mire kíváncsi (így nem kell a teljes mintatérrel
    # együtt korrigálni, különben ezek sosem lennének láthatók):
    # - a tovább bontott kontextusok szűkítései;
    # - a kiemelt dimenziók egyelemes mintái (pl. "ez a service önmagában kiugró"), saját szinttel.
    def family(pattern: Pattern) -> str:
        if references.get(pattern, (0.0, None))[1] is not None:
            return "context"
        if len(pattern) == 1 and pattern[0][0] in params.emphasized:
            return "emphasized"
        return "general"

    families = [family(pattern) for pattern in tested]
    q_values = [0.0] * len(tested)
    for name in set(families):
        members = [i for i, f in enumerate(families) if f == name]
        for i, q in zip(members, benjamini_hochberg([p_values[i] for i in members])):
            q_values[i] = q
    q_limits = {pattern: params.emphasized_fdr_q if f == "emphasized" else params.fdr_q for pattern, f in zip(tested, families)}

    significant: Dict[Pattern, PatternResult] = {}
    for pattern, p_value, q_value in zip(tested, p_values, q_values):
        if len(bad_ids.get(pattern, ())) < params.min_support:
            continue
        lift = lift_of(pattern)
        if q_value > q_limits[pattern] or lift < params.min_lift:
            continue
        reference_rate, context = references.get(pattern, (base_rate, None))
        extra = None
        if context is not None and params.blocked_paths:
            parent_dims = {d for d, _ in context}
            if any(path_blocked(parent_dims, d, params.blocked_paths) for d, _ in pattern if d not in parent_dims):
                continue   # az elemző szerint ez a bontási út zsákutca
        if context is not None:
            context_lift = (len(bad_ids[pattern]) / group_size[pattern]) / reference_rate
            # a kontextuson belül a magas alaparány miatt 2× ritkán érhető el: a szignifikancia a szűrő,
            # és legalább annyival kell erősebbnek lennie, mint bármely általánosítással szemben
            if context_lift < params.min_improvement:
                continue
            extra = {"context": pattern_key(context), "context_rate": reference_rate, "context_lift": context_lift}
        dimensions = {dimension for dimension, _ in pattern}
        if dimensions & RECURRING_DIMENSIONS and "date" not in dimensions:
            if len(bad_days[pattern]) < params.min_distinct_days:
                continue
        size, bad = group_size[pattern], len(bad_ids[pattern])
        significant[pattern] = PatternResult(
            measure=measure.key,
            items=pattern,
            bad_count=bad,
            group_size=size,
            population=total,
            base_rate=base_rate,
            rate=bad / size,
            lift=lift,
            expected=size * base_rate,
            excess=bad - size * base_rate,
            p_value=p_value,
            q_value=q_value,
            distinct_days=len(bad_days[pattern]),
            bad_incident_ids=tuple(sorted(set(bad_ids[pattern]))),
            extra=extra,
        )

    # 3. Redundanciaszűrés
    # a) összetett vagy dátumos minta csak akkor, ha érdemben erősebb minden általánosításánál
    #    (a nem számlált kombinációk, pl. dátum & hét napja, liftje 0, így nem számítanak)
    def suppressed_ok(pattern: Pattern, lift: float) -> bool:
        rest = tuple(item for item in pattern if item[0] not in params.suppressed)
        if rest == pattern:
            return True
        return bool(rest) and lift >= params.suppressed_improvement * lift_of(rest)

    kept = {
        pattern: result for pattern, result in significant.items()
        if all(result.lift >= params.min_improvement * lift_of(sub) for sub in _generalizations(pattern))
        and suppressed_ok(pattern, result.lift)
    }
    # b) általános minta kiesik, ha egy megmaradt szűkítése magyarázza a rossz ticketjeinek nagy részét
    explained = set()
    for pattern, result in kept.items():
        for other, other_result in kept.items():
            if other != pattern and set(pattern) < set(with_implied(other)):
                if other_result.bad_count >= params.explained_share * result.bad_count:
                    explained.add(pattern)
                    break
    results = [result for pattern, result in kept.items() if pattern not in explained]

    results.sort(key=_rank)
    return [_prefer_full_dimensions(r, params.suppressed) for r in merge_equivalent(results)][: params.top_n]


def _prefer_full_dimensions(result: PatternResult, suppressed: FrozenSet[str]) -> PatternResult:
    """Azonos esethalmaz leírásai közül a szűkítő (alap)mezőt nem tartalmazók maradnak: ha ugyanazt a halmazt
    egy csoport is leírja, az "impact=4 & ..." leírás csak zaj. Ha csak szűkítő mezős leírás van, az marad.

    Hívja: mining.mine."""
    if not suppressed:
        return result
    def clean(items: Pattern) -> bool:
        return not any(dim in suppressed for dim, _ in items)
    descriptions = [result.items, *result.alternatives]
    preferred = [d for d in descriptions if clean(d)]
    if not preferred:
        return result
    head = result.items if clean(result.items) else preferred[0]
    return replace(result, items=head, alternatives=tuple(d for d in preferred if d != head))


def _rank(result: PatternResult):
    """Erősebb előbb; egyenlőségnél a rövidebb (egyszerűbb) leírás, végül betűrend (determinisztikus).

    Hívja: mining.merge_equivalent (hivatkozás), mining.mine (hivatkozás)."""
    return (result.p_value, -result.excess, len(result.items), result.key)


def merge_equivalent(results: Sequence[PatternResult]) -> List[PatternResult]:
    """A pontosan azonos rossz ticket-halmazt leíró mintákat egyetlen eredménnyé vonja össze.

    A képviselő a legerősebb (ennek kulcsa adja a jelölt azonosítóját), a többi leírás
    alternatívaként marad meg: a gép nem tudja eldönteni, melyik mező a lényeg, az elemző igen.
    

    Hívja: mining.mine."""
    groups: Dict[Tuple[str, FrozenSet[str]], List[PatternResult]] = {}
    for result in sorted(results, key=_rank):
        groups.setdefault((result.measure, frozenset(result.bad_incident_ids)), []).append(result)
    merged = []
    for members in groups.values():
        head, rest = members[0], members[1:]
        merged.append(replace(head, alternatives=tuple(member.items for member in rest)))
    merged.sort(key=_rank)
    return merged
