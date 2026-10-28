from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewParams:
    rule_min_age_days: int = 14       # ennyi idős, soha nem talált szabály jelez
    rule_stale_days: int = 30         # ennyi ideje nem talált szabály jelez
    fact_review_days: int = 90        # ennyi ideje megerősítetlen tény jelez
    lookback_days: int = 30           # az ellentmondások ennyi napra visszamenőleg számítanak
    min_contradictions: int = 3       # legalább ennyi ellentmondó esemény kell
