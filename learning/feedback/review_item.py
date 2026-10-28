from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewItem:
    kind: str           # rule | fact
    object_id: str
    reason: str         # never_hit | stale | expired | unconfirmed | contradicted | unseen
    message: str
    evidence: int = 0   # ellentmondó események / találatok száma
