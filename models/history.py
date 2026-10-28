import hashlib
import json
from typing import Any, Dict


class HistoryLine:
    """Egyetlen történeti esemény (Audit Log sor) szigorú, validált leképezése."""

    def __init__(self, raw_data: Dict[str, Any]):
        if not raw_data or not isinstance(raw_data, dict):
            raise ValueError("❌ [HIBA] A HistoryLine konstruktor nem kapott érvényes szótárat (raw_data üres vagy nem Dict).")
            
        self.raw_data: Dict[str, Any] = raw_data

        # Szigorú validáció: ha a kötelező alapmezők hiányoznak, azonnal kivételt dobunk
        if not self.set:
            raise ValueError(f"❌ [HIBA] Hiányzó incidens azonosító ('set' / 'incident') a HistoryLine adatokban: {raw_data}")
        if not self.timestamp:
            raise ValueError(f"❌ [HIBA] Hiányzó időbélyeg ('timestamp' / 'update_time') a HistoryLine adatokban: {raw_data}")

    @property
    def history_id(self) -> str:
        supplied_id = self.raw_data.get("history_id") or self.raw_data.get("id")
        if supplied_id:
            return str(supplied_id)
        identity = {
            "set": self.set,
            "timestamp": str(self.timestamp),
            "field": self.field,
            "old": str(self.old),
            "new": str(self.new),
        }
        digest = hashlib.sha1(
            json.dumps(identity, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        return f"HIST-{digest}"

    def __getitem__(self, key: str) -> Any:
        return self.raw_data.get(key)

    @property
    def set(self) -> Any:
        return self.raw_data.get("set") or self.raw_data.get("incident") or self.raw_data.get("incident_id")

    @property
    def timestamp(self) -> Any:
        return (
            self.raw_data.get("timestamp")
            or self.raw_data.get("update_time")
            or self.raw_data.get("sys_created_on")
        )

    @property
    def field(self) -> str:
        return str(self.raw_data.get("field") or self.raw_data.get("element") or "")

    @property
    def old(self) -> Any:
        val = self.raw_data.get("old_value")
        if val is None or str(val).strip() in ("", "none"):
            val = self.raw_data.get("old")
        return val

    @property
    def old_value(self) -> Any:
        return self.old

    @property
    def new(self) -> Any:
        val = self.raw_data.get("new_value")
        if val is None or str(val).strip() in ("", "none"):
            val = self.raw_data.get("new")
        return val

    @property
    def new_value(self) -> Any:
        return self.new

    def to_dict(self) -> Dict[str, Any]:
        """Tiszta szótár konverzió minimalista mezőkkel a mentéshez."""
        return {
            "history_id": self.history_id,
            "set": self.set,
            "timestamp": self.timestamp,
            "field": self.field,
            "old": self.old,
            "old_value": self.old,
            "new": self.new,
            "new_value": self.new,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HistoryLine":
        return cls(raw_data=data)

    def __repr__(self):
        return f"<HistoryLine id='{self.history_id}' field='{self.field}' old='{self.old}' new='{self.new}'>"