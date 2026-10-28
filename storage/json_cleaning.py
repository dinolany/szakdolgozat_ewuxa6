from datetime import date, datetime
import math
import numbers
from typing import Any


def clean_nan(value: Any):
    """JSON-kompatibilissé tisztítja a DTO-k hiányzó és nem véges értékeit."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, numbers.Real):
        numeric_value = float(value)
        return numeric_value if math.isfinite(numeric_value) else None
    if value.__class__.__name__ in {"NaTType", "NAType"}:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: clean_nan(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [clean_nan(item) for item in value]
    if hasattr(value, "item"):
        try:
            return clean_nan(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "to_dict"):
        return clean_nan(value.to_dict())
    return str(value)
