"""Szintetikus előtörténet a valódi idősor elé, és összevetés a valódi adattal.

Használat:
    python build_synthetic.py                         # 12 hét, seed=1 -> synthetic_timeline.json
    python build_synthetic.py --weeks 16 --seed 7 --swap 0.3
    python build_synthetic.py --out x.json --no-compare
    python build_synthetic.py --faults default        # hibák beültetése (synthetic/faults.py forgatókönyvei)
    python build_synthetic.py --faults scenarios.json # saját forgatókönyvek

A sablonok a nyers Excelből jönnek vágás nélkül (a teljes lefutással); a valódi rész
a transformed_timeline.json változatlanul. Az eredmény a konzolon futtatható:
    python discovery_cli.py --timeline synthetic_timeline.json --state synthetic_state.json
"""

import argparse
import json
import sys
from collections import Counter
from datetime import timedelta
from pathlib import Path

from loader import load_excel
from synthetic.compare import incidents_from_rows, report, window_summary
from synthetic.faults import inject, load_scenarios
from synthetic.generator import GeneratorParams, build_templates, generate, merge_timelines
from time_transformer import TimeTransformer

BASE_DIR = Path(__file__).resolve().parent


class _UncutTransformer(TimeTransformer):
    """Ugyanaz a leképezés és deduplikálás, de a 2 hetes vége nélkül: teljes lefutások."""

    VIRTUAL_END = TimeTransformer.VIRTUAL_START + timedelta(days=100 * 365)


def main() -> None:
    parser = argparse.ArgumentParser(description="Szintetikus előtörténet a valódi idősor elé")
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--swap", type=float, default=0.5, help="más lefutást kapó ticketek aránya")
    parser.add_argument("--volume", type=float, default=1.0, help="érkezési ütem szorzója")
    parser.add_argument("--excel", default=str(BASE_DIR / "data.xlsx"))
    parser.add_argument("--real", default=str(BASE_DIR / "transformed_timeline.json"))
    parser.add_argument("--out", default=str(BASE_DIR / "synthetic_timeline.json"))
    parser.add_argument("--no-compare", action="store_true")
    parser.add_argument("--faults", help="hibák beültetése: 'default' vagy forgatókönyv-fájl (JSON lista)")
    parser.add_argument("--l1", help="az L1 csoport sys_id-ja (alapból a leggyakoribb nyitó csoport)")
    parser.add_argument("--with-clean", action="store_true",
                        help="a hibák nélküli változatot is elmenti (<out>.clean.json) a backtest tiszta összevetéséhez")
    args = parser.parse_args()

    print("[sablonok] nyers Excel betöltése vágás nélkül...", flush=True)
    templates = build_templates(_UncutTransformer().map_to_virtual_timeline(load_excel(args.excel)))
    print(f"[sablonok] {len(templates)} incidens teljes lefutással")

    real_rows = json.loads(Path(args.real).read_text(encoding="utf-8"))["rows"]
    start = TimeTransformer.VIRTUAL_START - timedelta(weeks=args.weeks)
    params = GeneratorParams(weeks=args.weeks, seed=args.seed, swap_share=args.swap, volume_scale=args.volume)
    synthetic_rows, summary = generate(templates, start, TimeTransformer.VIRTUAL_END, params)
    print(f"[generálás] {summary['incidents']} incidens, {summary['rows']} sor, "
          f"{summary['swapped_lifecycles']} cserélt lefutás ({start.date()} → {TimeTransformer.VIRTUAL_START.date()})")

    meta = {"synthetic": summary, "real_source": str(Path(args.real).name),
            "real_start": TimeTransformer.VIRTUAL_START.isoformat(), "end": TimeTransformer.VIRTUAL_END.isoformat()}
    if args.faults and args.with_clean:
        clean_path = Path(args.out).with_suffix(".clean.json")
        clean_path.write_text(json.dumps({"meta": {**meta, "clean_of": Path(args.out).name},
                                          "rows": merge_timelines(synthetic_rows, real_rows)}, ensure_ascii=False, default=str),
                              encoding="utf-8")
        print(f"[mentve] tiszta összevetés: {clean_path}")
    if args.faults:
        l1 = args.l1 or Counter(t.group for t in templates).most_common(1)[0][0]
        scenarios = load_scenarios(None if args.faults == "default" else args.faults)
        synthetic_rows, manifest = inject(synthetic_rows, scenarios, start, TimeTransformer.VIRTUAL_END, [l1], args.seed)
        manifest.update({"l1": l1, "synthetic_start": start.isoformat()})
        faults_path = Path(args.out).with_suffix(".faults.json")
        faults_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        meta["faults"] = faults_path.name
        for scenario in manifest["scenarios"]:
            print(f"[hiba] {scenario['name']:24} {scenario['kind']:13} csoport={scenario['group'][:12]:12} "
                  f"{scenario['start'][:10]} → {scenario['end'][:10]} ({scenario['shape']}): {scenario['applied']} beültetés")
        print(f"[hiba] ground truth: {faults_path}")
    rows = merge_timelines(synthetic_rows, real_rows)
    Path(args.out).write_text(json.dumps({"meta": meta, "rows": rows}, ensure_ascii=False, default=str),
                              encoding="utf-8")
    print(f"[mentve] {args.out} ({len(rows)} sor)")

    if not args.no_compare:
        incidents = incidents_from_rows(rows)
        second = timedelta(seconds=1)
        real_start, real_end = TimeTransformer.VIRTUAL_START, TimeTransformer.VIRTUAL_END - second
        span = TimeTransformer.VIRTUAL_END - TimeTransformer.VIRTUAL_START
        synthetic_start = TimeTransformer.VIRTUAL_START - span
        print("\nÖsszevetés: valódi két hét vs. az előtte lévő két szintetikus hét (a végükön mérve)")
        print(report(window_summary(incidents, real_start, real_end),
                     window_summary(incidents, synthetic_start, real_start - second)))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
