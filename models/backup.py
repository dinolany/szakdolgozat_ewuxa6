"""A tanult tudás mentése: datált másolatok és visszaállítás.

A „memória” két fájlban él: a tudástár (`knowledge.json`: tények, almérések, küszöbök, bontási szempontok, munkaidő,
tanulságok) és a feltárási állapot (`discovery_state.json`: szabályok, döntések, jelöltek). Minden mentés után – ha az
előző másolat óta legalább `min_interval` eltelt – a két fájl együtt egy datált mappába kerül; naponta egy külön,
hosszabb ideig megtartott másolat is készül. Visszaállításkor a kiválasztott mappa fájljai kerülnek vissza.
"""

import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

KEEP_RECENT = 30      # ennyi friss másolat marad
KEEP_DAILY = 60       # ennyi napi másolat marad
MIN_INTERVAL = 600    # két friss másolat között legalább ennyi másodperc


def _snapshots(backup_dir: Path) -> List[Path]:
    """A mentésmappák időrendben.

    Hívja: backup._prune, backup.list_snapshots, backup.snapshot."""
    return sorted((p for p in backup_dir.iterdir() if p.is_dir()), key=lambda p: p.name) if backup_dir.exists() else []


def snapshot(files: Iterable[Path], backup_dir: Path, force: bool = False, label: str = "",
             min_interval: int = MIN_INTERVAL) -> Optional[str]:
    """A fájlok együttes másolata; a mappa neve, vagy None, ha most nem kellett (túl friss az előző).

    Hívja: DiscoveryService._backup."""
    files = [Path(f) for f in files if f and Path(f).exists()]
    if not files:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    recent = [p for p in _snapshots(backup_dir) if not p.name.startswith("napi-")]
    made = None
    if force or not recent or time.time() - recent[-1].stat().st_mtime >= min_interval:
        made = now.strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")
        target = backup_dir / made
        target.mkdir(exist_ok=True)
        for f in files:
            shutil.copy2(f, target / f.name)
    daily = backup_dir / f"napi-{now:%Y%m%d}"
    if not daily.exists():
        daily.mkdir()
        for f in files:
            shutil.copy2(f, daily / f.name)
    _prune(backup_dir)
    return made


def _prune(backup_dir: Path) -> None:
    """A régi mentések törlése (a frissekből és a napiakból a megtartandó számú marad).

    Hívja: backup.snapshot."""
    all_snapshots = _snapshots(backup_dir)
    recent = [p for p in all_snapshots if not p.name.startswith("napi-")]
    daily = [p for p in all_snapshots if p.name.startswith("napi-")]
    for old in recent[:-KEEP_RECENT] + daily[:-KEEP_DAILY]:
        shutil.rmtree(old, ignore_errors=True)


def list_snapshots(backup_dir: Path) -> List[Dict[str, object]]:
    """A mentések listája (név, idő, fájlok, méret), a legújabb elöl.

    Hívja: DiscoveryService.backup_overview."""
    result = []
    for p in reversed(_snapshots(backup_dir)):
        files = sorted(f.name for f in p.iterdir() if f.is_file())
        result.append({"name": p.name, "daily": p.name.startswith("napi-"),
                       "at": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                       "files": files, "size": sum(f.stat().st_size for f in p.iterdir() if f.is_file())})
    return result


def restore(backup_dir: Path, name: str, targets: Iterable[Path]) -> List[str]:
    """A mappa fájljainak visszamásolása a helyükre (a fájlnév szerint); a visszaállított fájlok neve.

    Hívja: DiscoveryService.backup_action."""
    source = backup_dir / name
    if not source.is_dir() or source.parent.resolve() != backup_dir.resolve():
        raise ValueError(f"Nincs ilyen mentés: {name}")
    restored = []
    for target in targets:
        target = Path(target)
        candidate = source / target.name
        if candidate.exists():
            temp = target.with_suffix(target.suffix + ".tmp")
            shutil.copy2(candidate, temp)
            temp.replace(target)
            restored.append(target.name)
    return restored
