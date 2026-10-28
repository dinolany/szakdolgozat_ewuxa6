from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from learning.children.child_def import ChildDef
from learning.children.child_runner import MIN_UNITS, describe_result, run_child
from learning.children.unit_context import UnitRef
from model.timeline.ticket_timeline import TicketTimeline
from model.working_time.schedule import Schedule


# Gyári próbák: amikor az elemző tovább bontást kér, a gép ezeket (és a már tanított alméréseket) maga is kipróbálja
# az érintett eseteken, és a leginkább eltérőket felajánlja módszerként.
PROBES: Dict[str, Tuple[str, str]] = {
    "gyors_tovabbitas": ("Rögtön továbbadta (30 percen belül)", "tovabbitotta and szakasz_perc < 30"),
    "gyors_lezaras": ("Rögtön lezárta (30 percen belül)", "lezarta and szakasz_perc < 30"),
    "nincs_interakcio": ("Érdemi lépés nélkül", "interakciok == 0"),
    "felvetel_utan_azonnal": ("Névre vétel után rögtön lezárta / továbbadta", 'elso_lepes_felvetel_utan in ["lezaras", "tovabbitas"]'),
    "nem_vettek_nevre": ("Nem vették névre", 'elso_lepes_felvetel_utan == ""'),
    "egy_csoport": ("Egy csoport intézte", "egy_csoport"),
    "ujranyitva": ("Újranyitották", "ujranyitva"),
    "sok_valtas": ("Sok csoportváltás (3+)", "ujrakiosztasok >= 3"),
    "hold": ("Volt hold-ban", "hold_perc > 0"),
    "lezarasi_megjegyzes": ("Van lezárási megjegyzés", 'kitoltve("close_notes")'),
    "elutasitva": ("Elutasítva", "elutasitva"),
    "meg_nyitott": ("Még nyitott", "nyitott"),
    "hosszu_jegy": ("Egy napnál hosszabb jegy", "jegy_perc > 1440"),
    "munkaidon_kivul_kapta": ("Munkaidőn kívül kapta", "kapta_munkaidoben == False"),
    "hetvegen_nyilt": ("Hétvégén nyílt", "nyitas_hetvege"),
    "l1_adta_ra": ("Az L1 adta rá", 'elozo_szint == "L1"'),
}

# Gyári csoportosítások: mi szerint sűrűsödnek az érintett esetek
GROUP_PROBES: Dict[str, Tuple[str, str]] = {
    "service_szerint": ("Service szerint", 'ertek("business_service")'),
    "rado_csoport_szerint": ("Ráadó csoport szerint", "elozo_csoport"),
    "kapta_nap_szerint": ("A megkapás napja szerint", "kapta_nap"),
    "kapta_ora_szerint": ("A megkapás órája szerint", "kapta_ora"),
    "kategoria_szerint": ("Kategória szerint", 'ertek("category")'),
}

# a minta saját mezőjét bontó csoportosítás magától értetődő (pl. egy service-mintánál a service szerinti)
GROUP_PROBE_DIMENSIONS = {"service_szerint": "business_service", "kategoria_szerint": "category"}

PROBE_MIN_DIFFERENCE = 0.15   # ennyivel (arányban) kell eltérnie az érintetteknél a többiekhez képest

PROBE_TOP = 5

def run_probes(probes: Sequence[Tuple[str, ...]], affected: Sequence[UnitRef], reference: Sequence[UnitRef],
               timeline_of: Callable[[str], Optional[TicketTimeline]], levels: Mapping[str, str], now: str,
               min_difference: float = PROBE_MIN_DIFFERENCE, top: int = PROBE_TOP,
               schedules: Optional[Mapping[str, Schedule]] = None) -> List[Dict[str, Any]]:
    """(kulcs, forrás, név, kifejezés[, módszer]) próbák az érintett és az összehasonlító eseteken; a leginkább eltérők
    elöl. Csak az kerül be, ami elég érintett esetre kiértékelhető, és érdemben más az érintetteknél (csoportosításnál
    a legnagyobb eltérésű érték).

    Hívja: DiscoveryService._run_probes."""
    results = []
    for probe in probes:
        key, source, name, expression = probe[:4]
        method = probe[4] if len(probe) > 4 else "check"
        try:
            result = run_child(ChildDef(key, name, expression, method=method), affected, reference, timeline_of, levels, now,
                               schedules)
        except Exception:   # egy hibás próba nem akaszthatja meg a többit
            continue
        if result["affected_n"] < MIN_UNITS or result["share"] is None or result["reference_share"] is None:
            continue
        difference = result["share"] - result["reference_share"]
        if abs(difference) < min_difference:
            continue
        text = result["text"] if method == "group" else describe_result(
            name, "explain", result["affected_n"], result["affected_true"], result["reference_n"], result["reference_true"], False)
        results.append({**result, "probe": key, "source": source, "method": method, "difference": round(difference, 3), "text": text})
    results.sort(key=lambda r: -abs(r["difference"]))
    return results[:top]
