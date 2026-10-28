from datetime import datetime, timedelta
import threading
import time
import traceback
from typing import Any, Dict, List, TYPE_CHECKING

from simulation.playback_queue import PlaybackQueue
from simulation.time_transformer import TimeTransformer


if TYPE_CHECKING:
    from engine.event_processor import IncidentEventProcessor
    from storage.incident_repository import IncidentRepository


class SimulationScheduler:
    """3. RÉSZ: A szimuláció vezérlője (indítógomb). Saját adatot nem kezel:

    - adatot a TimeTransformertől kér (get_timeline), az esedékességet a PlaybackQueue mondja meg,
    - az esedékes eseménycsomagokat az IncidentEventProcessornak adja (process_events),
    - törléskor a repositoryt (clear) és a TimeTransformert (clear_cache) kéri meg.
    """

    TIME_FORMAT = "%d.%m.%Y %H:%M:%S"

    def __init__(
        self,
        transformer: TimeTransformer,
        playback_queue: PlaybackQueue,
        event_processor: "IncidentEventProcessor",
        repository: "IncidentRepository",
        on_reset: "Callable[[], None] | None" = None,
        on_day_end: "Callable[[datetime], Any] | None" = None,
    ):
        self.transformer = transformer
        # Reset után hívódik (pl. a feltárás állapotának törlése), bárhonnan jön a reset (konzol, dashboard)
        self.on_reset = on_reset
        self.on_day_end = on_day_end   # napvégi megállásnál (pl. a napi feltárás a nap végi állapotra)
        self.playback_queue = playback_queue
        self.event_processor = event_processor
        self.repository = repository

        self._lock = threading.RLock()  # állapotmezők védelme (rövid ideig tartjuk)
        # A start/stop/reset sorba rendezése: egy második start vagy egy reset megvárja a futót
        self._control_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # Minden start/reset növeli; egy régebbi futás szála ebből látja, hogy már nem érvényes
        self._generation = 0

        self._state = "stopped"  # stopped | running | paused | finished | error
        self._duration_seconds = 600
        self._bounds: Dict[str, Any] = {"start": None, "end": None}
        self._total = 0
        self._processed = 0
        self._current_virtual_time: datetime | None = None
        self._error: str | None = None
        self._processing = False

        # Fali óra a lejátszáshoz; a szünetben töltött idő nem számít bele
        self._started_wall = 0.0
        self._paused_total = 0.0
        self._paused_at: float | None = None
        # Napvégi léptetés: eddig a virtuális időpontig fut, ott megáll (szünet)
        self._pause_at: datetime | None = None
        self._pause_reason: str | None = None

    # --- VEZÉRLŐ FÜGGVÉNYEK ---

    def start(self, duration_seconds: int = 600) -> Dict[str, Any]:
        """Elindítja a szimulációt. duration_seconds=0 esetén minden esemény egyben, szinkron fut le."""
        if duration_seconds < 0:
            raise ValueError("A lejátszási időtartam nem lehet negatív.")

        with self._control_lock:
            return self._start(duration_seconds)

    def _start(self, duration_seconds: int) -> Dict[str, Any]:
        self._stop()

        events = self.playback_queue.load(self.transformer.get_timeline())
        bounds = self.playback_queue.get_bounds()

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._duration_seconds = duration_seconds
            self._bounds = bounds
            self._total = len(events)
            self._processed = 0
            self._current_virtual_time = bounds["start"]
            self._error = None
            self._processing = False
            self._started_wall = time.monotonic()
            self._paused_total = 0.0
            self._paused_at = None
            self._stop_event.clear()
            self._state = "running"

        if not events:
            with self._lock:
                self._state = "finished"
            return self.get_status()

        if duration_seconds == 0:
            if self._dispatch(events, generation, len(events)):
                with self._lock:
                    self._current_virtual_time = bounds["end"]
                    self._state = "finished"
            return self.get_status()

        self._thread = threading.Thread(target=self._run_loop, args=(generation,), daemon=True)
        self._thread.start()
        return self.get_status()

    def change_source(self, name: str) -> Dict[str, Any]:
        """Lejátszási forrás váltása (pl. szintetikus idősor): a mostani lejátszás adatai törlődnek
        (mint a resetnél; a tudástár marad), mert két forrás eseményei nem keveredhetnek."""
        select = getattr(self.transformer, "select", None)
        if select is None:
            raise ValueError("Ennél a lejátszónál nem választható forrás.")
        with self._control_lock:
            available = {s["name"]: s for s in self.transformer.available()}
            if name not in available or not available[name]["available"]:
                select(name)   # érthető hibát ad (ismeretlen vagy még nincs legyártva)
            self._reset()
            select(name)
            return self.get_status()

    def step_day(self, duration_seconds: int = 600) -> Dict[str, Any]:
        """Lejátszás a (virtuális) nap végéig, ott megáll. Leállított állapotból elindítja a lejátszást;
        szünetből folytatja; futás közben a mostani nap végén áll meg. A megállás pillanatában lefut a
        napvégi hívás (pl. a napi feltárás), így a tanuló kör a nap végi állapotot mutatja."""
        with self._control_lock:
            with self._lock:
                state = self._state
            if state in {"finished", "error"}:
                return self.get_status()
            if state == "stopped":
                if duration_seconds <= 0:
                    raise ValueError("Napvégi léptetéshez valós idejű lejátszás kell (10 perc vagy 1 óra).")
                self._start(duration_seconds)
            with self._lock:
                current = self._current_virtual_time
                end = self._bounds["end"]
                if current is None or self._state not in {"running", "paused"}:
                    return self.get_status()
                target = datetime.combine(current.date() + timedelta(days=1), datetime.min.time())
                self._pause_at = target if end is None or target < end else None
                self._pause_reason = None
            return self.resume()

    def pause(self) -> Dict[str, Any]:
        with self._lock:
            if self._state == "running":
                self._state = "paused"
                self._paused_at = time.monotonic()
        return self.get_status()

    def resume(self) -> Dict[str, Any]:
        with self._lock:
            if self._state == "paused":
                self._paused_total += time.monotonic() - self._paused_at
                self._paused_at = None
                self._state = "running"
                self._pause_reason = None
        return self.get_status()

    def stop(self) -> Dict[str, Any]:
        """Leállítja a futást, és megvárja, amíg a folyamatban lévő feldolgozás befejeződik."""
        with self._control_lock:
            return self._stop()

    def _stop(self) -> Dict[str, Any]:
        self._stop_event.set()
        with self._lock:
            if self._state in {"running", "paused"}:
                self._state = "stopped"
            self._paused_at = None
            self._pause_at = None
            self._pause_reason = None
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self._thread = None
        return self.get_status()

    def reset(self) -> Dict[str, Any]:
        """Leállít, majd az adatkezelőket kéri meg a törlésre: adatbázis és timeline cache."""
        with self._control_lock:
            return self._reset()

    def _reset(self) -> Dict[str, Any]:
        self._stop()
        self.repository.clear()
        self.transformer.clear_cache()
        self.playback_queue.clear()
        if self.on_reset is not None:
            self.on_reset()

        with self._lock:
            self._generation += 1
            self._state = "stopped"
            self._bounds = {"start": None, "end": None}
            self._total = 0
            self._processed = 0
            self._current_virtual_time = None
            self._error = None
            self._processing = False
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        source = self.transformer.status() if hasattr(self.transformer, "status") else {}
        with self._lock:
            return {**source,
                "state": self._state,
                "duration_seconds": self._duration_seconds,
                "started_at": self._format_time(self._bounds["start"]),
                "current_time": self._format_time(self._current_virtual_time),
                "end_time": self._format_time(self._bounds["end"]),
                "processed": self._processed,
                "total": self._total,
                "progress": (self._processed / self._total) if self._total else 0,
                "error": self._error,
                "processing": self._processing,
                "pause_at": self._format_time(self._pause_at),
                "pause_reason": self._pause_reason,
            }

    # --- BELSŐ IDŐSZIMULÁCIÓS CIKLUS ---

    def _run_loop(self, generation: int) -> None:
        start_sim = self._bounds["start"]
        total_sim_seconds = (self._bounds["end"] - start_sim).total_seconds()

        while not self._stop_event.is_set():
            with self._lock:
                if generation != self._generation or self._state not in {"running", "paused"}:
                    return
                paused = self._state == "paused"
                day_end = None
                if not paused:
                    progress = min(self._elapsed_wall() / self._duration_seconds, 1.0)
                    current_virt = start_sim + timedelta(seconds=total_sim_seconds * progress)
                    if self._pause_at is not None and current_virt >= self._pause_at:
                        day_end = current_virt = self._pause_at   # pontosan a nap végén áll meg
                        progress = (day_end - start_sim).total_seconds() / total_sim_seconds if total_sim_seconds else 1.0
                    self._current_virtual_time = current_virt
                    processed = self._processed

            if paused:
                self._stop_event.wait(0.1)
                continue

            due_events, next_idx = self.playback_queue.pop_due_events(current_virt, processed)
            if due_events and not self._dispatch(due_events, generation, next_idx):
                return

            if day_end is not None:
                self._finish_day(generation, day_end, progress)
                continue

            if progress >= 1.0:
                with self._lock:
                    if generation == self._generation and self._state == "running":
                        self._state = "finished"
                return

            self._stop_event.wait(0.1)

    def _finish_day(self, generation: int, day_end: datetime, progress: float) -> None:
        """Napvégi megállás: a napvégi hívás (feltárás), majd szünet pontosan a nap végén (a falióra-idő
        úgy igazodik, hogy a folytatás innen menjen tovább, ne ugorjon előre)."""
        if self.on_day_end is not None:
            with self._lock:
                self._processing = True
            try:
                self.on_day_end(day_end)
            except Exception:
                traceback.print_exc()
            finally:
                with self._lock:
                    self._processing = False
        with self._lock:
            if generation != self._generation or self._state != "running":
                return
            now = time.monotonic()
            self._paused_at = now
            self._paused_total = now - self._started_wall - progress * self._duration_seconds
            self._current_virtual_time = day_end
            self._pause_at = None
            self._pause_reason = "day_end"
            self._state = "paused"

    def _dispatch(self, events: List[Any], generation: int, next_idx: int) -> bool:
        """Átadja az eseménycsomagot a feldolgozónak. False, ha hiba történt vagy a futás közben érvénytelenné vált."""
        with self._lock:
            self._processing = True
        try:
            self.event_processor.process_events(events)
        except Exception as error:
            traceback.print_exc()
            with self._lock:
                self._processing = False
                if generation == self._generation:
                    self._error = str(error)
                    self._state = "error"
            return False

        with self._lock:
            self._processing = False
            if generation != self._generation:
                return False
            self._processed = next_idx
        return True

    def _elapsed_wall(self) -> float:
        now = time.monotonic()
        paused = self._paused_total + ((now - self._paused_at) if self._paused_at is not None else 0.0)
        return now - self._started_wall - paused

    @classmethod
    def _format_time(cls, value: Any) -> str | None:
        if isinstance(value, datetime):
            return value.strftime(cls.TIME_FORMAT)
        return value
