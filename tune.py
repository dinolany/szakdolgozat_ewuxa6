"""Paraméterhangolás a backtesten: paraméter-rácson méri a felismerést és a téves riasztást.

Használat:
    python tune.py --timeline synthetic_timeline.json --config config/discovery.json \\
        --grid measures.stint_pickup=8,16,24 --grid queue.lookback_days=7,14 --grid mining.min_lift=2,3
    python tune.py ... --grid usage.window.lookback_minutes=20160,43200 --grid usage.threshold.rare_rate=0.1,0.2

A "usage." kezdetű paraméterek a FEATURE_USAGE diagnosztikáé (window / threshold). A --config az alap
beállítás; a hangolás egy másolaton fut, az éles fájlt nem írja.

Kombinációnként: felismert forgatókönyvek, átlagos késés, átlagos stabilitás, téves riasztás (a tiszta
futás jelöltjei naponta). A javasolt beállítás: a zajhatár alatt a legtöbb felismert forgatókönyv,
azon belül a legkisebb átlagos késés. A tiszta változat (.clean.json) kell hozzá.
"""

import argparse
import copy
import itertools
import json
import sys
from pathlib import Path

from models.discovery.config import DiscoveryConfig
from synthetic.backtest import run_backtest
from synthetic.compare import incidents_from_rows


def parse_grid(entries):
    grid = []
    for entry in entries:
        name, _, values = entry.partition("=")
        section, _, key = name.partition(".")
        if not key or not values:
            raise SystemExit(f"Hibás rács: {entry} (szekció.kulcs=érték,érték)")
        grid.append((section, key, [float(v) for v in values.split(",")]))
    return grid


def summarize(result):
    scenarios = [s for s in result["scenarios"] if s.group != "*"]
    detected = [s for s in scenarios if s.first_detected is not None]
    systemwide = [s for s in result["scenarios"] if s.group == "*"]
    return {
        "detected": len(detected),
        "scenarios": len(scenarios),
        "mean_delay": sum(s.delay_days for s in detected) / len(detected) if detected else None,
        "mean_stability": sum(s.stability for s in scenarios) / len(scenarios) if scenarios else 0.0,
        "noise": result["noise"]["clean_total"],
        "decisions": result["noise"]["clean_decisions"],
        "systemwide_groups": sum(len(s.flagged_groups) for s in systemwide),
        "per_scenario": {s.name: {"delay": s.delay_days, "stability": round(s.stability, 2)} for s in scenarios},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Paraméterhangolás a backtesten")
    parser.add_argument("--timeline", required=True)
    parser.add_argument("--config", help="az alap beállítás (pl. config/discovery.json); csak olvassa")
    parser.add_argument("--grid", action="append", default=[], help="szekció.kulcs=érték1,érték2,...")
    parser.add_argument("--max-noise", type=float, default=None,
                        help="a tiszta futás elemzői döntéseinek felső határa naponta (csomópontokkal); "
                             "alapból a tartós döntési kapacitás (queue.decision_capacity)")
    parser.add_argument("--step", type=int, default=2, help="ennyi naponta mér (a rácson a 2 is elég)")
    parser.add_argument("--out", help="az összes eredmény JSON-ban")
    args = parser.parse_args()

    grid = parse_grid(args.grid or ["measures.stint_pickup=8,16,24", "queue.lookback_days=7,14"])
    if args.max_noise is None:
        args.max_noise = float((DiscoveryConfig(args.config) if args.config else DiscoveryConfig()).get("queue", "decision_capacity"))
    timeline = Path(args.timeline)
    manifest = json.loads(timeline.with_suffix(".faults.json").read_text(encoding="utf-8"))
    clean_path = timeline.with_suffix(".clean.json")
    if not clean_path.exists():
        raise SystemExit("A hangoláshoz kell a tiszta változat: build_synthetic.py --with-clean")
    print("[adat] betöltés...", flush=True)
    incidents = incidents_from_rows(json.loads(timeline.read_text(encoding="utf-8"))["rows"])
    clean = incidents_from_rows(json.loads(clean_path.read_text(encoding="utf-8"))["rows"])

    base = DiscoveryConfig(args.config) if args.config else DiscoveryConfig()
    rows = []
    for values in itertools.product(*(options for _, _, options in grid)):
        config = DiscoveryConfig()                 # fájl nélküli: az update nem ír semmit
        config.values = copy.deepcopy(base.values)
        usage = {"threshold": {}, "window": {}}
        setting = {}
        for (section, key, _), value in zip(grid, values):
            if section == "usage":
                kind, _, name = key.partition(".")
                usage[kind][name] = value
                setting[f"usage.{key}"] = value
            else:
                setting[f"{section}.{key}"] = config.update(section, key, value)
        summary = summarize(run_backtest(incidents, manifest, config, clean, step_days=args.step, usage=usage))
        rows.append({"setting": setting, **summary})
        delay = f"{summary['mean_delay']:.1f}" if summary["mean_delay"] is not None else "-"
        print(f"{setting}: felismert {summary['detected']}/{summary['scenarios']}, késés {delay} nap, "
              f"stabilitás {summary['mean_stability']:.0%}, téves riasztás {summary['noise']:.1f}/nap "
              f"({summary['decisions']:.1f} döntés)", flush=True)

    allowed = [r for r in rows if r["decisions"] <= args.max_noise]
    if allowed:
        best = max(allowed, key=lambda r: (r["detected"], -(r["mean_delay"] if r["mean_delay"] is not None else 1e9)))
        print(f"\nJavaslat (téves döntés <= {args.max_noise}/nap): {best['setting']}")
    else:
        print(f"\nEgyik kombináció sem fér a {args.max_noise}/nap zajhatár alá; a legkisebb zaj: "
              f"{min(r['decisions'] for r in rows):.1f} döntés/nap – a keresési paramétereket (min_lift, fdr_q, min_support) is hangolni kell.")
    if args.out:
        Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
