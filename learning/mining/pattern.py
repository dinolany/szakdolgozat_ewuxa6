from typing import Iterable, Tuple


Item = Tuple[str, str]


Pattern = Tuple[Item, ...]


def pattern_key(items: Iterable[Item]) -> str:
    """A minta kanonikus kulcsa: rendezett 'dimenzió=érték' párok '&'-tel.

    Hívja: DiscoveryManager.ingest_signals, DiscoveryService._refinements, DiscoveryService.knowledge_overview,
    PatternFindingDiagnosis.observe_definition, PatternResult.alternative_keys, PatternResult.key,
    knowledge.match_rule, mining.mine, pattern_finding.finding_subject."""
    return "&".join(f"{dimension}={value}" for dimension, value in sorted(items))
