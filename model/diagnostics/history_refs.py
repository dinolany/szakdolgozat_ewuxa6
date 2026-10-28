from typing import Any, Dict


def make_history_ref(incident_id: str, history_line: Any, reason: str = "") -> Dict[str, Any]:
    """Létrehoz egy szabványos hivatkozást egy HistoryLine eseményre a diagnosztikák számára."""
    # Ha dict-ként érkezne vészhelyzetben, de a HistoryLine objektumot várjuk
    if isinstance(history_line, dict):
        timestamp = history_line.get("timestamp") or history_line.get("update_time")
        field = history_line.get("field", "")
        old_val = history_line.get("old_value") or history_line.get("old")
        new_val = history_line.get("new_value") or history_line.get("new")
        history_id = history_line.get("history_id")
    else:
        timestamp = history_line.timestamp
        field = history_line.field
        old_val = history_line.old
        new_val = history_line.new
        history_id = history_line.history_id

    return {
        "incident_id": str(incident_id),
        "history_id": history_id,
        "timestamp": timestamp,
        "field": field,
        "old_value": old_val,
        "new_value": new_val,
        "reason": reason,
    }