from dataclasses import dataclass
from datetime import datetime

from model.history_line import HistoryLine


@dataclass(frozen=True)
class FieldChange:
    at: datetime
    field: str          # egységesített mezőnév
    old: str
    new: str
    event: HistoryLine  # az eredeti history sor (bizonyítéknak)
