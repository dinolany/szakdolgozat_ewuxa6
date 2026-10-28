"""Backtest a beültetett hibákon: mikor ismeri fel őket a feltárás és a diagnosztika.

Használat:
    python build_synthetic.py --faults default --with-clean --out synthetic_timeline.json
    python backtest.py --timeline synthetic_timeline.json [--step 1] [--config config/discovery.json]

A ground truth a timeline mellett: synthetic_timeline.faults.json; a hibák nélküli változat (ha van):
synthetic_timeline.clean.json – ezzel a téves riasztás is mérhető.
"""

import argparse
import json
import sys
from pathlib import Path

from models.discovery.config import DiscoveryConfig
from synthetic.backtest import report, run_backtest
from synthetic.compare import incidents_from_rows

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest a beültetett hibákon")
    parser.add_argument("--timeline", default=str(BASE_DIR / "synthetic_timeline.json"))
    parser.add_argument("--faults", help="ground truth (alapból a timeline melletti .faults.json)")
    parser.add_argument("--clean", help="a hibák nélküli változat (alapból a timeline melletti .clean.json, ha van)")
    parser.add_argument("--config", help="feltárási paraméterek (alapból az alapértékek)")
    parser.add_argument("--step", type=int, default=1, help="ennyi naponta mér")
    parser.add_argument("--out", help="az eredmény JSON-ban (napi bontással)")
    args = parser.parse_args()

    faults_path = Path(args.faults) if args.faults else Path(args.timeline).with_suffix(".faults.json")
    manifest = json.loads(faults_path.read_text(encoding="utf-8"))
    print(f"[adat] {args.timeline} betöltése...", flush=True)
    incidents = incidents_from_rows(json.loads(Path(args.timeline).read_text(encoding="utf-8"))["rows"])
    print(f"[adat] {len(incidents)} incidens; {len(manifest['scenarios'])} forgatókönyv, {len(manifest['truth'])} beültetés", flush=True)
    config = DiscoveryConfig(args.config) if args.config else DiscoveryConfig()
    clean_path = Path(args.clean) if args.clean else Path(args.timeline).with_suffix(".clean.json")
    clean = None
    if clean_path.exists():
        clean = incidents_from_rows(json.loads(clean_path.read_text(encoding="utf-8"))["rows"])
        print(f"[adat] tiszta összevetés: {clean_path.name}", flush=True)
    else:
        print("[adat] nincs tiszta változat: a téves riasztás nem mérhető (build_synthetic.py --with-clean)", flush=True)

    def progress(as_of, faulty, clean_count):
        print(f"  {as_of.date()} {faulty} jelölt" + (f" (tiszta: {clean_count})" if clean_count is not None else ""), flush=True)

    result = run_backtest(incidents, manifest, config, clean, step_days=args.step, progress=progress)
    print("\n" + report(result))
    if args.out:
        serializable = {**result, "scenarios": [{**s.__dict__, "start": s.start.isoformat(), "end": s.end.isoformat(),
                                                 "first_detected": s.first_detected.isoformat() if s.first_detected else None,
                                                 "delay_days": s.delay_days} for s in result["scenarios"]]}
        Path(args.out).write_text(json.dumps(serializable, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
