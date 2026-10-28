from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class TrackedFault:
    name: str
    kind: str
    group: str
    start: datetime
    end: datetime
    first_seen: Optional[str] = None       # először volt döntésre váró jelölt hozzá
    first_shown: Optional[str] = None      # először került a napi keretbe (az elemző elé)
    accepted: Optional[str] = None         # finding lett
