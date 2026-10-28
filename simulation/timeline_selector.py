"""A lejátszás adatforrása: a valódi (Excelből transzformált) idősor, vagy egy szintetikus idősor.

A szintetikus idősort a `build_synthetic.py` gyártja (szintetikus előtörténet a valódi két hét elé,
opcionálisan beültetett hibákkal). Itt csak kiválasztjuk és betöltjük: a fájlt sosem töröljük
(a reset csak a valódi forrás gyorsítótárát üríti, ahogy eddig).

A scheduler számára ugyanaz a felület, mint a TimeTransformeré: get_timeline() és clear_cache().
"""

import json
from pathlib import Path
import threading
from typing import Any, Dict, List, Optional

from config.paths import DATA_DIR
from simulation.time_transformer import TimeTransformer


BASE_DIR = DATA_DIR

SOURCES: Dict[str, Dict[str, str]] = {
    "real": {"label": "Valódi adat (2 hét)", "description": "Az Excelből transzformált valódi idősor."},
    "synthetic": {"label": "Szintetikus előtörténet + beültetett hibák",
                  "description": "12 hét szintetikus előtörténet a valódi két hét előtt, beültetett hibákkal "
                                 "(megoldókulcs: a beültetett hibák listája).",
                  "path": "synthetic_timeline.json", "faults": "synthetic_timeline.faults.json"},
    "synthetic_clean": {"label": "Szintetikus előtörténet, hibák nélkül",
                        "description": "Ugyanaz a szintetikus előtörténet beültetett hibák nélkül (összevetéshez: "
                                       "ami itt is jelölt, az nem a beültetett hibák miatt jelent meg).",
                        "path": "synthetic_timeline.clean.json"},
}
GENERATE_HINT = "python -m simulation.synthetic.build_synthetic --faults default --with-clean"


class TimelineSelector:
    """Választható lejátszási forrás (a választás egy kis fájlban megmarad az újraindítások között)."""

    def __init__(self, transformer: TimeTransformer, base_dir: Path = BASE_DIR, state_file: str = "simulation_source.json"):
        self.transformer = transformer
        self.base_dir = Path(base_dir)
        self.state_path = self.base_dir / state_file
        self._lock = threading.Lock()
        self._cache: Optional[tuple] = None   # (útvonal, módosítási idő, sorok)
        self.source = self._load_choice()

    # --- választás ---

    def _load_choice(self) -> str:
        try:
            name = json.loads(self.state_path.read_text(encoding="utf-8")).get("source", "real")
        except (OSError, ValueError):
            return "real"
        return name if name in SOURCES and self._exists(name) else "real"

    def _path(self, name: str, key: str = "path") -> Optional[Path]:
        relative = SOURCES[name].get(key)
        return self.base_dir / relative if relative else None

    def _exists(self, name: str) -> bool:
        path = self._path(name)
        return path is None or path.exists()

    def available(self) -> List[Dict[str, Any]]:
        result = []
        for name, info in SOURCES.items():
            path = self._path(name)
            item = {"name": name, "label": info["label"], "description": info["description"], "available": self._exists(name)}
            if path is not None and path.exists():
                item["size_mb"] = round(path.stat().st_size / 1e6)
            elif path is not None:
                item["hint"] = f"Még nincs legyártva: {GENERATE_HINT}"
            result.append(item)
        return result

    def select(self, name: str) -> None:
        if name not in SOURCES:
            raise ValueError(f"Ismeretlen forrás: {name} (lehetséges: {', '.join(SOURCES)}).")
        if not self._exists(name):
            raise ValueError(f"A(z) „{SOURCES[name]['label']}” forrás még nincs legyártva. Futtasd: {GENERATE_HINT}")
        with self._lock:
            self.source = name
            self._cache = None
        try:
            self.state_path.write_text(json.dumps({"source": name}), encoding="utf-8")
        except OSError:
            pass

    # --- a scheduler felülete ---

    def get_timeline(self) -> List[Dict[str, Any]]:
        if self.source == "real":
            return self.transformer.get_timeline()
        path = self._path(self.source)
        with self._lock:
            stamp = (str(path), path.stat().st_mtime_ns)
            if self._cache is None or self._cache[:2] != stamp:
                print(f"--> [FORRÁS] Szintetikus idősor betöltése: {path.name}...", flush=True)
                rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
                rows = TimeTransformer._from_serializable(rows)
                rows.sort(key=lambda row: row.get("_virtual_dt"))
                self._cache = (*stamp, rows)
                print(f"--> [FORRÁS] {len(rows)} sor betöltve.", flush=True)
            return self._cache[2]

    def clear_cache(self) -> None:
        """Reset: a valódi forrás gyorsítótára ürül (ahogy eddig); a szintetikus fájl megmarad."""
        if self.source == "real":
            self.transformer.clear_cache()

    # --- megoldókulcs ---

    def faults(self) -> Optional[Dict[str, Any]]:
        path = self._path(self.source, "faults") if self.source in SOURCES else None
        if path is None or not path.exists():
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        keep = ("name", "kind", "group", "start", "end", "shape", "applied", "description")
        return {"scenarios": [{k: s.get(k) for k in keep} for s in manifest.get("scenarios", [])],
                "l1": manifest.get("l1")}

    def status(self) -> Dict[str, Any]:
        return {"source": self.source, "source_label": SOURCES[self.source]["label"], "sources": self.available(),
                "faults": self.faults()}
