from datetime import datetime
from typing import Any, Dict, List, Tuple


class PlaybackQueue:
    """A szimuláció lejátszási sora (élesben nincs rá szükség).

    A TimeTransformer rendezett idősorát tárolja nyers sorokként, és megmondja, mely események
    esedékesek egy adott virtuális időpontig. Eseményobjektumot (HistoryLine) nem épít:
    azt az IncidentEventProcessor teszi meg a csomag átvételekor.
    """

    TIME_KEY = "_virtual_dt"

    def __init__(self):
        self._events: List[Dict[str, Any]] = []

    def load(self, timeline: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Betölti a (virtuális idő szerint rendezett) idősort lejátszásra."""
        self._events = list(timeline)
        return self._events

    def clear(self) -> None:
        self._events = []

    def __len__(self) -> int:
        return len(self._events)

    def get_bounds(self) -> Dict[str, Any]:
        if not self._events:
            return {"start": None, "end": None}
        return {
            "start": self._events[0].get(self.TIME_KEY),
            "end": self._events[-1].get(self.TIME_KEY),
        }

    def pop_due_events(
        self, current_virtual_time: datetime, last_processed_index: int
    ) -> Tuple[List[Dict[str, Any]], int]:
        """A kurzortól kezdve az összes esemény, amelynek ideje <= current_virtual_time; és az új kurzor."""
        due_events = []
        idx = last_processed_index

        while idx < len(self._events):
            event_dt = self._events[idx].get(self.TIME_KEY)
            if event_dt and event_dt <= current_virtual_time:
                due_events.append(self._events[idx])
                idx += 1
            else:
                break

        return due_events, idx
