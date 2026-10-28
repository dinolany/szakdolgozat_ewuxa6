import ast
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from learning.children.child_def import ChildDef
from learning.children.expression_language import GROUP_ROWS, compile_expression, evaluate, evaluate_value
from learning.children.unit_context import UnitContext, UnitRef
from model.timeline.stint import Stint
from model.timeline.ticket_timeline import TicketTimeline
from model.working_time.schedule import Schedule


MIN_UNITS = 3            # ennél kevesebb érintett esetből nem mondunk ítéletet

MAX_UNITS = 2000         # egy oldalon legfeljebb ennyi esetet értékelünk ki (determinisztikus ritkítással)

def _thin(units: Sequence[UnitRef]) -> List[UnitRef]:
    """Legfeljebb MAX_UNITS eset (determinisztikus ritkítással), hogy egy almérés nagy adaton se legyen lassú.

    Hívja: child_runner.measure_groups, child_runner.measure_units."""
    units = sorted(set(units), key=lambda u: (u[0], -1 if u[1] is None else u[1]))
    if len(units) <= MAX_UNITS:
        return units
    step = len(units) / MAX_UNITS
    return [units[int(i * step)] for i in range(MAX_UNITS)]

def measure_units(tree: ast.Expression, units: Sequence[UnitRef], timeline_of: Callable[[str], Optional[TicketTimeline]],
                  levels: Mapping[str, str], schedules: Optional[Mapping[str, Schedule]] = None) -> Tuple[int, int, List[str]]:
    """(kiértékelt esetek, ahol igaz, néhány incidens ahol igaz).

    Hívja: child_runner.run_child."""
    n = hits = 0
    examples: List[str] = []
    for incident_id, index in _thin(units):
        timeline = timeline_of(incident_id)
        if timeline is None:
            continue
        stint = None
        if index is not None:
            if index >= len(timeline.stints):
                continue
            stint = timeline.stints[index]
        n += 1
        if evaluate(tree, UnitContext(timeline, stint, levels, schedules)):
            hits += 1
            if len(examples) < 3:
                examples.append(incident_id)
    return n, hits, examples

def measure_groups(tree: ast.Expression, units: Sequence[UnitRef], timeline_of: Callable[[str], Optional[TicketTimeline]],
                   levels: Mapping[str, str], schedules: Optional[Mapping[str, Schedule]] = None) -> Tuple[int, Dict[str, int], Dict[str, List[str]]]:
    """Csoportosítás: (kiértékelt esetek, értékenkénti darabszám, értékenként néhány incidens).

    Hívja: child_runner._run_group."""
    n = 0
    counts: Dict[str, int] = {}
    examples: Dict[str, List[str]] = {}
    for incident_id, index in _thin(units):
        timeline = timeline_of(incident_id)
        if timeline is None:
            continue
        stint = None
        if index is not None:
            if index >= len(timeline.stints):
                continue
            stint = timeline.stints[index]
        try:
            value = evaluate_value(tree, UnitContext(timeline, stint, levels, schedules))
        except Exception:
            continue
        n += 1
        counts[value] = counts.get(value, 0) + 1
        if len(examples.setdefault(value, [])) < 3:
            examples[value].append(incident_id)
    return n, counts, examples

def _run_group(child: ChildDef, affected, reference, timeline_of, levels, now, schedules) -> Dict[str, Any]:
    """Csoportosító almérés: az érintett esetek hogyan oszlanak meg az érték szerint, a többihez képest. Teljesül, ha az
    érintettek legalább `threshold` hányada egyetlen értékre esik (az eltérés egy csoportba sűrűsödik).

    Hívja: child_runner.run_child."""
    tree = compile_expression(child.expression)
    n, counts, examples = measure_groups(tree, affected, timeline_of, levels, schedules)
    rn, rcounts, _ = measure_groups(tree, reference, timeline_of, levels, schedules)
    rows = []
    for value, count in counts.items():
        share = count / n
        reference_share = rcounts.get(value, 0) / rn if rn else None
        rows.append({"value": value, "count": count, "share": round(share, 4),
                     "reference_count": rcounts.get(value, 0),
                     "reference_share": None if reference_share is None else round(reference_share, 4),
                     "difference": round(share - (reference_share or 0.0), 4), "examples": examples.get(value, [])})
    rows.sort(key=lambda r: (-r["count"], -r["difference"]))
    top = rows[0] if rows else None
    triggered = bool(n >= MIN_UNITS and top is not None and top["share"] >= child.threshold)
    widest = max(rows, key=lambda r: abs(r["difference"])) if rows else None
    parts = [f"{r['value'] or '(üres)'} {r['share']:.0%}" + (f" (a többinél {r['reference_share']:.0%})" if r["reference_share"] is not None else "")
             for r in rows[:GROUP_ROWS]]
    if n == 0:
        text = f"{child.name}: nincs kiértékelhető érintett eset."
    else:
        text = f"{child.name}: {len(counts)} érték az érintett {n} esetben – " + ", ".join(parts)
        if len(rows) > GROUP_ROWS:
            text += f", +{len(rows) - GROUP_ROWS} további"
        if n < MIN_UNITS:
            text += " – kevés eset az ítélethez."
        elif triggered:
            text += f" → az érintettek {top['share']:.0%}-a egy értékre esik ({top['value'] or '(üres)'})."
        else:
            text += "."
    return {
        "child_id": child.child_id, "name": child.name, "role": child.role, "expression": child.expression, "method": "group",
        "threshold": child.threshold, "affected_n": n, "affected_true": top["count"] if top else 0,
        # a próbálkozások rangsorához: a legnagyobb eltérésű érték aránya
        "share": widest["share"] if widest else None, "reference_n": rn,
        "reference_true": widest["reference_count"] if widest else 0,
        "reference_share": (widest["reference_share"] if widest and widest["reference_share"] is not None else (0.0 if widest else None)),
        "triggered": triggered, "examples": top["examples"] if top else [], "evaluated_at": now,
        "groups": rows[:GROUP_ROWS * 2], "text": text,
    }

def with_previous(results: List[Dict[str, Any]], previous: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Az előző futás aránya mellé (pl. figyelés után visszatérve látszik, mi változott).

    Hívja: DiscoveryManager.store_child_results."""
    before = {r.get("child_id"): r for r in previous or ()}
    for result in results:
        old = before.get(result["child_id"])
        if old is not None and old.get("evaluated_at") != result.get("evaluated_at"):
            result["previous_share"] = old.get("share")
            result["previous_at"] = old.get("evaluated_at")
        elif old is not None and "previous_share" in old:
            result["previous_share"], result["previous_at"] = old["previous_share"], old.get("previous_at")
    return results

def run_child(child: ChildDef, affected: Sequence[UnitRef], reference: Sequence[UnitRef],
              timeline_of: Callable[[str], Optional[TicketTimeline]], levels: Mapping[str, str], now: str,
              schedules: Optional[Mapping[str, Schedule]] = None) -> Dict[str, Any]:
    """Egy almérés lefuttatása: az érintett és az összehasonlító eseteken mért arány (ellenőrzésnél) vagy megoszlás
    (csoportosításnál), a teljesülés és a szöveges eredmény.

    Hívja: DiscoveryService._run_children, DiscoveryService.children_action, probes.run_probes."""
    if child.method == "group":
        return _run_group(child, affected, reference, timeline_of, levels, now, schedules)
    tree = compile_expression(child.expression)
    n, hits, examples = measure_units(tree, affected, timeline_of, levels, schedules)
    rn, rhits, _ = measure_units(tree, reference, timeline_of, levels, schedules)
    share = hits / n if n else None
    reference_share = rhits / rn if rn else None
    triggered = bool(n >= MIN_UNITS and share is not None and share >= child.threshold)
    return {
        "child_id": child.child_id, "name": child.name, "role": child.role, "expression": child.expression,
        "threshold": child.threshold, "affected_n": n, "affected_true": hits, "share": share,
        "reference_n": rn, "reference_true": rhits, "reference_share": reference_share,
        "triggered": triggered, "examples": examples, "evaluated_at": now,
        "text": describe_result(child.name, child.role, n, hits, rn, rhits, triggered),
    }

def describe_result(name: str, role: str, n: int, hits: int, rn: int, rhits: int, triggered: bool) -> str:
    """Egy ellenőrző almérés eredménye szavakban (arány az érintetteknél és a többinél, ítélet).

    Hívja: child_runner.run_child, probes.run_probes."""
    if n == 0:
        return f"{name}: nincs kiértékelhető érintett eset."
    text = f"{name}: az érintett esetek {hits / n:.0%}-ára igaz ({hits}/{n})"
    text += f", a többinél {rhits / rn:.0%} ({rhits}/{rn})" if rn else ", összehasonlító eset nincs"
    if n < MIN_UNITS:
        return text + " – kevés eset az ítélethez."
    if triggered:
        return text + (" → magyarázat: valószínűleg helyes működés." if role == "explain" else " → riasztás.")
    return text + "."

def derived_evaluator(derived: Sequence[Tuple[str, ...]], levels: Mapping[str, str],
                      schedules: Optional[Mapping[str, Schedule]] = None):
    """(dimenzió, kifejezés[, módszer]) hármasokból egy függvény: (idővonal, szakasz) -> {dimenzió: érték}. Ellenőrzésnél
    az érték "igen" / "nem", csoportosításnál a kifejezés értéke (üres érték nem kerül a leírásba).
    A hibás kifejezés kimarad (a feltárás nem állhat meg miatta).

    Hívja: profiles.build_profiles."""
    compiled = []
    for entry in derived:
        name, expression = entry[0], entry[1]
        method = entry[2] if len(entry) > 2 else "check"
        try:
            compiled.append((name, compile_expression(expression), method))
        except ValueError:
            continue

    def values(timeline: TicketTimeline, stint: Optional[Stint]) -> Dict[str, str]:
        unit = UnitContext(timeline, stint, levels, schedules)
        result = {}
        for name, tree, method in compiled:
            try:
                if method == "group":
                    value = evaluate_value(tree, unit)
                    if value:
                        result[name] = value
                else:
                    result[name] = "igen" if evaluate(tree, unit) else "nem"
            except Exception:  # egy eset kiértékelési hibája ne akassza meg a többit
                continue
        return result

    return values
