from enum import Enum
from typing import Optional


# Az ablakparaméterek kötelező kulcsai ablaktípusonként
LOOKBACK_KEY = "lookback_minutes"

BUCKET_KEY = "bucket_minutes"

class WindowType(str, Enum):
    NONE = "NONE"          # az incidens teljes élete, nincs időablak
    SNAPSHOT = "SNAPSHOT"  # állapotkép a mérés pillanatában
    SLIDING = "SLIDING"    # [vég - lookback, vég], mérésről mérésre csúszik
    TUMBLING = "TUMBLING"  # igazított, egymást nem fedő vödrök

    def __str__(self):
        return self.value

    @property
    def is_timed(self) -> bool:
        """Időablakos-e a mérés (ezeknél létezik OUTDATED állapot)."""
        return self in (WindowType.SLIDING, WindowType.TUMBLING)

    @property
    def length_key(self) -> Optional[str]:
        return {WindowType.SLIDING: LOOKBACK_KEY, WindowType.TUMBLING: BUCKET_KEY}.get(self)
