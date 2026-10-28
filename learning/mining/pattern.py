from typing import Iterable, Tuple


Item = Tuple[str, str]


Pattern = Tuple[Item, ...]


def pattern_key(items: Iterable[Item]) -> str:
    """A minta kanonikus kulcsa: rendezett 'dimenzió=érték' párok '&'-tel.

    Hívja: DiscoveryManager.ingest_signals, DiscoveryService._refinements, DiscoveryService.knowledge_overview,
    PatternFindingDiagnosis.observe_definition, PatternResult.alternative_keys, PatternResult.key,
    rule.match_rule, contrast_mining.mine, pattern_finding_diagnosis.finding_subject."""
    return "&".join(f"{dimension}={value}" for dimension, value in sorted(items))
