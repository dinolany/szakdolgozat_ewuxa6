from dataclasses import replace
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from models.discovery.baseline import pattern_counts
from models.discovery.config import BASELINE_SECTION, QUEUE_SECTION, TREND_SECTION, DiscoveryConfig
from models.discovery.knowledge import Candidate, CandidateState, make_candidate_id
from models.discovery.manager import DiscoveryManager, IngestReport
from models.discovery.measures import MEASURES, MEASURES_BY_KEY
from models.discovery.mining import PatternResult, mine
from models.discovery.profile import ProfileSet, build_profiles
from models.discovery.trend import mine_trends
from models.incident import Incident


def discover(
    incidents: Iterable[Incident],
    as_of: datetime,
    config: DiscoveryConfig,
    levels: Optional[Mapping[str, str]] = None,
    known_bad: Optional[Mapping[str, Set[str]]] = None,
    derived: Sequence[Tuple[str, str]] = (),
) -> Tuple[List[PatternResult], datetime, Dict[str, int]]:
    """Feltárás az `as_of` időpontban: a visszatekintési ablakban nyitott ticketeken és az ablakban
    kezdődött csoportszakaszokon, minden mérésre.

    Visszaadja a mintákat, az ablak kezdetét és mérésenként a populáció méretét.
    

    Hívja: backtest._day_signals."""
    window_start = as_of - timedelta(days=config.get(QUEUE_SECTION, "lookback_days"))
    profiles = build_profiles(incidents, as_of, window_start, levels, derived)
    results, populations = _mine_all(profiles, config)
    results.extend(_mine_trends(incidents, as_of, window_start, config, levels, profiles, known_bad))
    return results, window_start, populations


def _mine_trends(incidents, as_of: datetime, window_start: datetime, config: DiscoveryConfig, levels, current: ProfileSet,
                 known_bad: Optional[Mapping[str, Set[str]]] = None) -> List[PatternResult]:
    """Romlás a saját múlthoz képest: a múlt az ablak előtti hetek, a MOSTANI pillanatban mérve.

    Az ablak kezdetén mérve a közvetlenül előtte érkezett egységek még nem futhattak le (pl. 16 órás
    felvétel), így a múlt mesterségesen "tiszta" lenne. Most mérve a múlt teljes; a torzítás a mostani ablak
    friss egységeinél marad, az pedig óvatos irányba hat (kevesebb jelzés).

    Hívja: runner.discover, runner.run_discovery."""
    if not config.get(TREND_SECTION, "enabled"):
        return []
    params = config.trend_params()
    span = build_profiles(incidents, as_of, window_start - timedelta(weeks=params.history_weeks), levels)
    history = ProfileSet(tuple(p for p in span.tickets if p.opened_at < window_start),
                         tuple(p for p in span.stints if p.opened_at < window_start))
    # legalább egy teljes ablaknyi múlt kell: pár nap adat nem "saját norma", csak pillanatkép
    earliest = min((p.opened_at for p in history.tickets), default=window_start)
    if window_start - earliest < (as_of - window_start):
        return []
    results: List[PatternResult] = []
    for measure in MEASURES:
        results.extend(mine_trends(current.for_measure(measure), history.for_measure(measure), measure, params,
                                   config.measure_threshold(measure.key), (known_bad or {}).get(measure.key, ())))
    return results


def _mine_all(profiles: ProfileSet, config: DiscoveryConfig,
              contexts: Optional[Mapping[str, Sequence[Tuple[Tuple[str, str], ...]]]] = None,
              blocked_paths: Iterable[Tuple[str, str]] = (),
              ) -> Tuple[List[PatternResult], Dict[str, int]]:
    """Mérésenként a mintakeresés (a kontextusokkal és a tiltott utakkal) és a romlás-keresés eredménye.

    Hívja: runner.discover, runner.run_discovery."""
    params = replace(config.mining_params(), blocked_paths=frozenset(blocked_paths))
    results: List[PatternResult] = []
    populations: Dict[str, int] = {}
    for measure in MEASURES:
        units = profiles.for_measure(measure)
        populations[measure.key] = sum(1 for p in units if p.measures.get(measure.key) is not None)
        results.extend(mine(units, measure, params, config.measure_threshold(measure.key),
                            (contexts or {}).get(measure.key, ())))
    return results, populations


def _counts(profiles: ProfileSet, measure_key: str, items, config: DiscoveryConfig) -> Dict[str, int]:
    """Egy minta darabszámai az ablakban (érintett, összes, populáció) az ismert eltérés normájához.

    Hívja: runner.backfill_counts, runner.run_discovery."""
    measure = MEASURES_BY_KEY[measure_key]
    return pattern_counts(profiles.for_measure(measure), measure, items, config.measure_threshold(measure_key))


def configure(manager: DiscoveryManager, config: DiscoveryConfig) -> None:
    """A kezelő paraméterei a beállításokból (erősség, keret, ismert eltérés, kiemelt mezők, jelzés-visszatérés).

    Hívja: DiscoveryCli.cmd_known, DiscoveryService.__init__, DiscoveryService._fresh_manager, DiscoveryService._run,
    DiscoveryService.backup_action, DiscoveryService.close_issue_with_lesson, DiscoveryService.generalize,
    DiscoveryService.overview, DiscoveryService.update_config és mások."""
    manager.extreme_strength = config.get(QUEUE_SECTION, "extreme_strength")
    manager.signal_return_days = config.get(QUEUE_SECTION, "watch_days")
    manager.decision_capacity = config.get(QUEUE_SECTION, "decision_capacity")
    manager.training_capacity = config.get(QUEUE_SECTION, "training_capacity")
    manager.training_decisions = config.get(QUEUE_SECTION, "training_decisions")
    manager.baseline_min_size = config.get(BASELINE_SECTION, "min_size")
    manager.baseline_prior = config.get(BASELINE_SECTION, "prior")
    manager.baseline_max_p = config.get(BASELINE_SECTION, "max_p")
    manager.baseline_min_ratio = config.get(BASELINE_SECTION, "min_ratio")
    manager.baseline_min_bad = config.get(BASELINE_SECTION, "min_bad")
    manager.emphasized_dimensions = frozenset(d for d, r in config.dimension_roles().items() if r == 3)


def run_discovery(
    manager: DiscoveryManager,
    incidents: Iterable[Incident],
    as_of: datetime,
    config: DiscoveryConfig,
) -> IngestReport:
    """A napi feltárás: adatlapok, mintakeresés és romlás-keresés, a jelöltek fogadása a kezelőbe.

    Hívja: DiscoveryCli.cmd_run, DiscoveryService._run, learning_sim._day."""
    configure(manager, config)
    window_start = as_of - timedelta(days=config.get(QUEUE_SECTION, "lookback_days"))
    view = manager.knowledge_view()
    levels = dict(view.levels)
    profiles = build_profiles(incidents, as_of, window_start, levels, view.derived, view.schedules)
    results, _ = _mine_all(profiles, config, manager.contexts(), manager.blocked_breakdown_paths())
    # az elfogadott findingok rossz ticketjei ismert probléma: nem lehetnek a saját múlt "normális" része
    known_bad: Dict[str, Set[str]] = {}
    for candidate in manager.candidates():
        if candidate.state == CandidateState.ACCEPTED.value:
            known_bad.setdefault(candidate.measure, set()).update(candidate.bad_incident_ids)
    results.extend(_mine_trends(incidents, as_of, window_start, config, levels, profiles, known_bad))

    # Darabszámok minden ismert és új jelöltre, a nem kiugróakra is (különben a szokásos eltérés
    # csak a kiugró hetekből állna, és felfelé torzulna)
    patterns = {c.candidate_id: (c.measure, c.items) for c in manager.candidates() if c.measure in MEASURES_BY_KEY}
    patterns.update({make_candidate_id(r.measure, r.candidate_key): (r.measure, r.items) for r in results})
    counts = {cid: _counts(profiles, measure, items, config) for cid, (measure, items) in patterns.items()}
    return manager.ingest(results, as_of, window_start, as_of, counts)


def backfill_counts(
    manager: DiscoveryManager,
    incidents: Sequence[Incident],
    candidate: Candidate,
    as_of: datetime,
    config: DiscoveryConfig,
) -> int:
    """A jelölt darabszámai a korábbi, egymást nem átfedő ablakokra (az `as_of` előtti időből).

    Minden ablak a végén látott állapotot méri, ugyanúgy, mint az akkori futás tette volna.
    Visszaadja a feltöltött ablakok számát (amelyben volt mérhető egység).
    

    Hívja: DiscoveryCli.cmd_known, DiscoveryService.close_issue_with_lesson."""
    lookback = timedelta(days=config.get(QUEUE_SECTION, "lookback_days"))
    view = manager.knowledge_view()
    levels = dict(view.levels)
    filled = 0
    for index in range(1, config.get(BASELINE_SECTION, "backfill_windows") + 1):
        window_end = as_of - index * lookback
        window_start = window_end - lookback
        profiles = build_profiles(incidents, window_end, window_start, levels, view.derived, view.schedules)
        values = _counts(profiles, candidate.measure, candidate.items, config)
        if values["population"]:
            manager.record_counts(candidate.candidate_id, window_start, window_end, values)
            filled += 1
    return filled
