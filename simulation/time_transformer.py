from collections import defaultdict
from datetime import datetime, timedelta
import json
import os
import pandas as pd
from typing import Any, Callable, Dict, List


class TimeTransformer:
    """A nyers audit logok transzformálása a 2026. augusztusi fiktív 2 hetes idősíkra.

    Önálló modul: egyedül ő kezeli a transformed_timeline.json cache-t (olvasás, írás, ürítés).
    A nyers adatot egy kívülről átadott forrásfüggvénytől kéri, így nem függ a loadertől.
    """

    VIRTUAL_START = datetime(2026, 8, 1)
    WINDOW_DAYS = 14
    VIRTUAL_END = VIRTUAL_START + timedelta(days=WINDOW_DAYS)  # kizárólagos felső határ

    # Növelni kell, ha a leképezés/vágás/dedup logikája változik: a régi cache így érvénytelenné válik
    CACHE_VERSION = 1

    # A transzformer által datetime-ra állított mezők (cache visszaolvasásnál is datetime-má alakítjuk)
    _TIME_KEYS = ("update_time", "sys_created_on", "timestamp", "_virtual_dt")

    def __init__(
        self,
        raw_source: Callable[[], List[Dict[str, Any]]] | None = None,
        output_filename: str = "transformed_timeline.json",
        source_fingerprint: Callable[[], str | None] | None = None,
    ):
        self.raw_source = raw_source
        self.output_filename = output_filename
        # A nyers forrás aktuális állapotát azonosító függvény (pl. Excel méret + módosítási idő)
        self.source_fingerprint = source_fingerprint

    # --- CACHE KEZELÉS (transformed_timeline.json) ---

    def get_timeline(self, force_rebuild: bool = False) -> List[Dict[str, Any]]:
        """A szimuláció egyetlen adatforrása: a cache-ből olvas, üres cache esetén újraépíti."""
        if not force_rebuild:
            cached = self._read_cache()
            if cached:
                return cached

        if self.raw_source is None:
            print("⚠️ [TRANSFORMER] A timeline üres, és nincs beállítva nyers adatforrás.", flush=True)
            return []

        print("--> [TRANSFORMER] Timeline generálása a nyers adatforrásból...", flush=True)
        fingerprint = self._current_fingerprint()
        serializable = self._to_serializable(self.map_to_virtual_timeline(self.raw_source()))
        self._write_cache(serializable, fingerprint)
        # Ugyanazon a normalizáláson megy át, mint a cache-ből olvasott adat
        return self._from_serializable(serializable)

    def clear_cache(self) -> None:
        try:
            with open(self.output_filename, "w", encoding="utf-8") as f:
                f.write("[]")
        except OSError as e:
            print(f" ❌ [HIBA] Nem sikerült kiüríteni a timeline fájlt ({self.output_filename}): {e}")

    def _read_cache(self) -> List[Dict[str, Any]]:
        """A cache tartalma, ha érvényes; üres lista, ha nincs, üres vagy elavult."""
        if not os.path.exists(self.output_filename):
            return []
        try:
            with open(self.output_filename, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if not content or content == "[]":
                return []
            data = json.loads(content)
        except (OSError, json.JSONDecodeError) as e:
            print(f"❌ [HIBA] Nem sikerült beolvasni a timeline fájlt: {e}")
            return []

        reason = self._stale_reason(data)
        if reason:
            print(f"--> [TRANSFORMER] A timeline cache elavult ({reason}), újraépítés szükséges.", flush=True)
            return []
        return self._from_serializable(data["rows"])

    def _cache_meta(self, fingerprint: str | None) -> Dict[str, Any]:
        return {
            "version": self.CACHE_VERSION,
            "virtual_start": self.VIRTUAL_START.isoformat(),
            "window_days": self.WINDOW_DAYS,
            "source_fingerprint": fingerprint,
        }

    def _current_fingerprint(self) -> str | None:
        return self.source_fingerprint() if self.source_fingerprint else None

    def _stale_reason(self, data: Any) -> str | None:
        """Miért nem használható a cache (None = érvényes)."""
        if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
            return "régi formátum, nincs fejléc"
        meta = data.get("meta") or {}
        expected = self._cache_meta(None)
        for key in ("version", "virtual_start", "window_days"):
            if meta.get(key) != expected[key]:
                return f"eltérő {key}: {meta.get(key)} != {expected[key]}"
        current = self._current_fingerprint()
        # Ha a forrás állapota most nem ismert (pl. nincs meg az Excel), a cache-t elfogadjuk
        if current is not None and meta.get("source_fingerprint") != current:
            return "a nyers forrás megváltozott"
        return None

    def _write_cache(self, serializable: List[Dict[str, Any]], fingerprint: str | None) -> None:
        temp_file = f"{self.output_filename}.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump({"meta": self._cache_meta(fingerprint), "rows": serializable}, f, indent=4, ensure_ascii=False)
            os.replace(temp_file, self.output_filename)
            print(f"--> [TRANSFORMER] Timeline mentve: {self.output_filename} ({len(serializable)} sor)", flush=True)
        except Exception as e:
            print(f"❌ [HIBA A MENTÉSNÉL]: {e}")
            import traceback
            traceback.print_exc()

    @staticmethod
    def _to_serializable(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        serializable = []
        for item in records:
            item_copy = {}
            for k, v in item.items():
                if isinstance(v, datetime):
                    item_copy[k] = v.isoformat()
                elif pd.isna(v):
                    item_copy[k] = None
                else:
                    item_copy[k] = v
            serializable.append(item_copy)
        return serializable

    @classmethod
    def _from_serializable(cls, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for item in records:
            for key in cls._TIME_KEYS:
                value = item.get(key)
                if isinstance(value, str) and value:
                    item[key] = datetime.fromisoformat(value)
        return records

    # --- IDŐLEKÉPEZÉS ---

    def _get_time(self, val: Any) -> datetime | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val
        if hasattr(val, "to_pydatetime"):
            try:
                return val.to_pydatetime()
            except Exception:
                pass
        if isinstance(val, str) and val.strip():
            try:
                return pd.to_datetime(val, dayfirst=True).to_pydatetime()
            except Exception:
                pass
        return None

    def _map_to_virtual_day(self, orig_dt: datetime) -> datetime:
        """A valós időpontot a virtuális idősík azonos nevű napjára teszi.

        Páratlan nap -> "A" hét (VIRTUAL_START + 0..6 nap), páros nap -> "B" hét (+7..13 nap).
        """
        week_start = self.VIRTUAL_START + timedelta(days=0 if orig_dt.day % 2 != 0 else 7)
        offset = (orig_dt.weekday() - week_start.weekday()) % 7  # 0 = Hétfő, ..., 6 = Vasárnap
        virtual_date = week_start + timedelta(days=offset)

        # Óra, perc, másodperc megmarad a valósából
        return virtual_date.replace(
            hour=orig_dt.hour,
            minute=orig_dt.minute,
            second=orig_dt.second,
            microsecond=orig_dt.microsecond
        )

    def map_to_virtual_timeline(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Tiszta transzformáció (nem ír fájlt): leképezés, 2 hetes vágás, deduplikálás, rendezés."""
        if not raw_records:
            return []

        parsed_records = []
        incident_times = defaultdict(list)

        # 1. Parsolás és incidensenkénti gyűjtés
        for rec in raw_records:
            item = rec.copy()
            dt = self._get_time(item.get("update_time"))
            item["_parsed_dt"] = dt

            inc_id = str(
                item.get("set")
                or item.get("number")
                or item.get("incident_id")
                or "UNKNOWN"
            ).strip()

            item["_inc_id"] = inc_id
            if dt:
                incident_times[inc_id].append(dt)

            parsed_records.append(item)

        # 2. Incidensenkénti delta kiszámítása a legelső bejegyzés alapján
        incident_deltas = {}
        for inc_id, times in incident_times.items():
            if not times:
                continue
            min_raw_time = min(times)
            virtual_min_time = self._map_to_virtual_day(min_raw_time)
            incident_deltas[inc_id] = virtual_min_time - min_raw_time

        # 3. Eltolás alkalmazása, és 2 hetes vágás: az idősíkon kívül eső vagy
        #    el nem helyezhető (időbélyeg / incidens azonosító nélküli) sorok kiesnek
        transformed = []
        dropped_out_of_window = 0
        dropped_unplaceable = 0
        for item in parsed_records:
            raw_dt = item.pop("_parsed_dt", None)
            inc_id = item.pop("_inc_id", "UNKNOWN")

            if not raw_dt or inc_id == "UNKNOWN" or inc_id not in incident_deltas:
                dropped_unplaceable += 1
                continue

            virtual_dt = raw_dt + incident_deltas[inc_id]
            if not (self.VIRTUAL_START <= virtual_dt < self.VIRTUAL_END):
                dropped_out_of_window += 1
                continue

            item["update_time"] = virtual_dt
            item["sys_created_on"] = virtual_dt
            item["timestamp"] = virtual_dt
            item["_virtual_dt"] = virtual_dt
            transformed.append(item)

        if dropped_out_of_window or dropped_unplaceable:
            print(
                f"--> [TRANSFORMER] Vágás: {dropped_out_of_window} sor a {self.WINDOW_DAYS} napos idősíkon kívül, "
                f"{dropped_unplaceable} sor időbélyeg/incidens azonosító nélkül.",
                flush=True,
            )

        # 4. CÉLZOTT DUPLIKÁCIÓ SZŰRÉS (Deduplication)
        seen_signatures = set()
        deduplicated_result = []

        for item in transformed:
            # 1. Incidens azonosító
            inc_id = str(
                item.get("set")
                or item.get("number")
                or item.get("incident_id")
                or ""
            ).strip()

            # 2. Idő (időbélyeg)
            v_dt = str(item.get("_virtual_dt") or item.get("update_time") or "")

            # 3. Mező (field)
            field = str(item.get("field") or item.get("element") or "").strip()

            # 4. Régi érték (biztosítva az old/old_value fallbacket)
            raw_old = item.get("old_value")
            if raw_old is None or str(raw_old).strip() in ("", "none"):
                raw_old = item.get("old")
            old_value = str(raw_old or "").strip()

            # 5. Új érték (biztosítva az new/new_value fallbacket)
            raw_new = item.get("new_value")
            if raw_new is None or str(raw_new).strip() in ("", "none"):
                raw_new = item.get("new")
            new_value = str(raw_new or "").strip()

            # Visszaírjuk az item-be is, hogy a HistoryLine garantáltan megkapja mindkettőt
            item["old_value"] = raw_old
            item["new_value"] = raw_new

            # Pontos aláírás a paraméterek alapján
            signature = (inc_id, v_dt, field, old_value, new_value)

            if signature not in seen_signatures:
                seen_signatures.add(signature)
                deduplicated_result.append(item)

        # 5. Rendezés a virtuális idő szerint
        return sorted(deduplicated_result, key=lambda x: x["_virtual_dt"])
