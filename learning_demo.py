"""Tanítási szimuláció a szintetikus adaton: a tanuló kör bemutatása kézi kattintgatás nélkül.

Használat:
    python build_synthetic.py --faults default --with-clean      # ha még nincs szintetikus adat
    python learning_demo.py                                      # -> learning_demo.json
    python learning_demo.py --days 10 --no-baseline              # gyors próba

A szimulált elemző a megoldókulcs (synthetic_timeline.faults.json) alapján dönt; részletek:
synthetic/learning_sim.py.
"""

import argparse
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

from synthetic.compare import incidents_from_rows
from synthetic.learning_sim import run_learning_sim

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Tanítási szimuláció a szintetikus adaton")
    parser.add_argument("--timeline", default=str(BASE_DIR / "synthetic_timeline.json"))
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "discovery.json"),
                        help="feltárási paraméterek (csak olvassa)")
    parser.add_argument("--days", type=int, help="csak ennyi napot játszik le (próba)")
    parser.add_argument("--no-baseline", action="store_true", help="tanulás nélküli összehasonlító kör nélkül")
    parser.add_argument("--out", default=str(BASE_DIR / "learning_demo.json"))
    args = parser.parse_args()

    manifest = json.loads(Path(args.timeline).with_suffix(".faults.json").read_text(encoding="utf-8"))
    print(f"[adat] {args.timeline} betöltése...", flush=True)
    incidents = incidents_from_rows(json.loads(Path(args.timeline).read_text(encoding="utf-8"))["rows"])
    print(f"[adat] {len(incidents)} incidens, {len(manifest['scenarios'])} beültetett hiba", flush=True)

    started = time.time()

    def progress(learning, baseline):
        extra = f" | tanulás nélkül: {baseline['waiting']:3} vár, {baseline['decisions']:2} döntés" if baseline else ""
        tip = f"{learning['tip_agreement']:.0%}" if learning["tip_agreement"] is not None else "-"
        print(f"  {learning['day']}  tanuló: {learning['waiting']:3} vár, {learning['decisions']:2} döntés "
              f"({learning['findings']} feladat, {learning['closed']} lezárás, {learning['attached']} csatolva), tipp {tip}, "
              f"szabály {learning['rules']}{extra}  [{time.time() - started:.0f} s]", flush=True)

    first = last = None
    if args.days:
        from datetime import datetime
        first = min(datetime.fromisoformat(s["start"]) for s in manifest["scenarios"]) - timedelta(days=21)
        last = first + timedelta(days=args.days - 1)
    result = run_learning_sim(incidents, manifest, args.config, first, last, with_baseline=not args.no_baseline,
                              progress=progress)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nBeültetett hibák:")
    for fault in result["faults"]:
        print(f"  {fault['name']:26} {fault['kind']:13} kezdet {fault['start'][:10]}  első jelölt {fault['first_seen'] or '-':10}"
              f"  elém került {fault['first_shown'] or '-':10}  feladat {fault['accepted'] or '-'}")
    baseline = result["tracks"].get("tanulás nélkül", {}).get("faults", {})
    if baseline:
        print("Tanulás nélkül:")
        for name, log in baseline.items():
            print(f"  {name:26} első jelölt {log.get('first_seen', '-'):10}  elém került {log.get('first_shown', '-'):10}  finding {log.get('accepted', '-')}")
    for name, track in result["tracks"].items():
        print(f"{name}: összesen {track['decisions']} döntés, {track['closed']} lezárás, {track['attached']} csatolt jelölt, "
              f"{track['rules']} aktív szabály, {len(track['generalizations'])} elfogadott általánosítás, "
              f"{len(track['suppressed_elements'])} elnyomott elem")
    print(f"[mentve] {args.out}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
