from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, Optional

# Az ablakparaméterek kötelező kulcsai ablaktípusonként
LOOKBACK_KEY = "lookback_minutes"
BUCKET_KEY = "bucket_minutes"

# A TUMBLING vödrök igazítási pontja (így az órás vödör egész órákra esik)
_EPOCH = datetime(1970, 1, 1)


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


def parse_timestamp(raw: Any) -> Optional[datetime]:
    """Időbélyeg értelmezése naiv datetime-ként; értelmezhetetlen értékre None."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None)
    text = str(raw).strip()
    if not text or text.upper() == "N/A":
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for pattern in ("%d.%m.%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _iso(value: Optional[datetime]) -> str:
    return value.isoformat() if value else ""


@dataclass(frozen=True)
class MeasurementWindow:
    """Egy konkrét mérés időablaka."""

    type: WindowType
    start: Optional[datetime] = None
    end: Optional[datetime] = None

    @classmethod
    def none(cls) -> "MeasurementWindow":
        return cls(WindowType.NONE)

    @classmethod
    def snapshot(cls, at: datetime) -> "MeasurementWindow":
        return cls(WindowType.SNAPSHOT, None, at)

    @classmethod
    def sliding(cls, end: datetime, lookback_minutes: float) -> "MeasurementWindow":
        return cls(WindowType.SLIDING, end - timedelta(minutes=lookback_minutes), end)

    @classmethod
    def tumbling(cls, at: datetime, bucket_minutes: float) -> "MeasurementWindow":
        """Az `at` időpontig utoljára lezárult vödör."""
        bucket = timedelta(minutes=bucket_minutes)
        end = _EPOCH + ((at - _EPOCH) // bucket) * bucket
        return cls(WindowType.TUMBLING, end - bucket, end)

    def resized(self, length_minutes: float) -> "MeasurementWindow":
        """Azonos végponthoz horgonyzott, új hosszúságú ablak (aktív issue újraméréséhez)."""
        if not self.type.is_timed or self.end is None:
            return self
        return MeasurementWindow(self.type, self.end - timedelta(minutes=length_minutes), self.end)

    def contains(self, moment: Optional[datetime]) -> bool:
        if self.type is WindowType.NONE:
            return True
        if moment is None:
            return False
        if self.start is not None and moment < self.start:
            return False
        if self.end is not None:
            # A TUMBLING vödör vége nyitott, hogy a szomszédos vödrök ne fedjék egymást
            if self.type is WindowType.TUMBLING and moment >= self.end:
                return False
            if moment > self.end:
                return False
        return True

    @property
    def key(self) -> str:
        return f"{self.type}:{_iso(self.start) or '-'}/{_iso(self.end) or '-'}"

    def to_dict(self) -> Dict[str, str]:
        return {"window_type": str(self.type), "window_start": _iso(self.start), "window_end": _iso(self.end)}

    @classmethod
    def from_values(cls, window_type: Any, start: Any, end: Any) -> "MeasurementWindow":
        try:
            parsed_type = WindowType(str(window_type))
        except ValueError:
            parsed_type = WindowType.NONE
        return cls(parsed_type, parse_timestamp(start), parse_timestamp(end))
