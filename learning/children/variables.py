"""Almérések (child diagnostic): az elemző által tanított, jelöltekre automatikusan lefutó részmérések.

A mezőegyezéses mintakeresés nem lát árnyalt összefüggést (pl. "azonnal lezárta, ezért elég a close
notes, munkajegyzet nem kell"). Az almérés egy esetre (jegyre vagy szakaszra) kiértékelt állítás, amelyet
az elemző ad meg, és megmondja, mire fusson (kiváltó) és mit jelent az eredménye (szerep):

- állítás: egy soros kifejezés egy szűk, biztonságos nyelven (nem Python-kód: csak az alábbi szókincs,
  logikai, összehasonlító és alapműveletek), vagy ugyanez adatsorként (`rows_to_expression`);
- kiváltó: melyik jelöltre fusson (egy jelölt, egy jelzésfajta, egy mintaelem, egy mérés, vagy mind);
- szerep: `explain` (magyarázat: ha az érintett esetek nagy részére igaz, a jelölt valószínűleg rendben
  van) vagy `alert` (riasztás: ha igaz, az a baj).

Az eredmény mindig összevetés: az érintett esetek hányadára igaz, és a többi (összehasonlító) eset hányadára.
"""

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from learning.children.unit_context import UnitContext, _minutes
from model.timeline.fields import CLOSED_STATES
from model.working_time.schedule import Schedule, WEEKDAY_LABELS


def _var_szakasz_perc(u: UnitContext):
    """Az almérés-nyelv „szakasz_perc” változó: az eset hossza percben (szakasznál a csoportnál töltött idő, ticketnél
    a lezárásig).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _minutes((u.end - u.start).total_seconds() / 3600)

def _var_jegy_perc(u: UnitContext):
    """Az almérés-nyelv „jegy_perc” változó: a teljes jegy élete percben (nyitástól az első lezárásig vagy máig).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    end = u.timeline.first_closed_at or u.timeline.as_of
    return _minutes((end - u.timeline.opened_at).total_seconds() / 3600)

def _var_felvetel_perc(u: UnitContext):
    """Az almérés-nyelv „felvetel_perc” változó: hány perc múlva lett felelőse (üres, ha nem lett).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    first = _pickup(u)
    return _minutes((first.at - u.start).total_seconds() / 3600) if first else None

def _var_hold_perc(u: UnitContext):
    """Az almérés-nyelv „hold_perc” változó: hold-ban töltött percek.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _minutes(u.stint.hold_hours if u.stint is not None else sum(h.hours for h in u.timeline.holds))

def _var_lezarta(u: UnitContext):
    """Az almérés-nyelv „lezarta” változó: a csoport (szakasznál) lezárta a jegyet / a jegy le lett zárva.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.stint.end_reason == "closed" if u.stint is not None else u.timeline.first_closed_at is not None

def _var_tovabbitotta(u: UnitContext):
    """Az almérés-nyelv „tovabbitotta” változó: a szakasz továbbítással ért véget.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.stint is not None and u.stint.end_reason == "transfer"

def _var_nyitott(u: UnitContext):
    """Az almérés-nyelv „nyitott” változó: még tart.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.stint.is_open if u.stint is not None else not u.timeline.is_closed

def _var_csoport(u: UnitContext):
    """Az almérés-nyelv „csoport” változó: a csoport (szakasznál az övé).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), variables._moment_felvette,
    variables._moment_kapta, variables._moment_vege, variables._var_felvetel_munkaperc, variables._var_munkarend_ismert,
    variables._var_szakasz_munkaperc, variables._var_szint."""
    return u.stint.group if u.stint is not None else u.timeline.value_at("assignment_group")

def _var_kovetkezo_csoport(u: UnitContext):
    """Az almérés-nyelv „kovetkezo_csoport” változó: továbbításnál a következő csoport.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.stint.next_group if u.stint is not None else ""

def _var_szint(u: UnitContext):
    """Az almérés-nyelv „szint” változó: a csoport szintje a tudástárból (pl. L1).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.levels.get(_var_csoport(u), "")

def _var_allapot(u: UnitContext):
    """Az almérés-nyelv „allapot” változó: az állapot (state) az eset végén, pl. '6' megoldott, '7' lezárt.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.timeline.value_at("state", u.end)

def _var_nyito_csoport(u: UnitContext):
    """Az almérés-nyelv „nyito_csoport” változó: a jegyet elsőként kapó csoport.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), variables._moment_nyitas,
    variables._var_nyito_szint."""
    return u.timeline.first_value("assignment_group")

def _var_zaro_csoport(u: UnitContext):
    """Az almérés-nyelv „zaro_csoport” változó: a jegyet lezáró csoport (üres, ha nincs lezárva).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    closing = u._closing_stint()
    return closing.group if closing else ""

def _var_csoportok_szama(u: UnitContext):
    """Az almérés-nyelv „csoportok_szama” változó: hány csoport érintette a jegyet.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return len(u.timeline.groups)

def _var_egy_csoport(u: UnitContext):
    """Az almérés-nyelv „egy_csoport” változó: csak egy csoport érintette a jegyet.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return len(u.timeline.groups) == 1

def _var_nyit_zar_egyedul(u: UnitContext):
    """Az almérés-nyelv „nyit_zar_egyedul” változó: egyetlen csoport kapta és zárta le (nem került máshoz).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return len(u.timeline.groups) == 1 and u.timeline.first_closed_at is not None

def _var_ujrakiosztasok(u: UnitContext):
    """Az almérés-nyelv „ujrakiosztasok” változó: csoportváltások száma (az L1-ből indulás nem számít).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return len(u.timeline.reassignments(u.levels))

def _var_ujranyitva(u: UnitContext):
    """Az almérés-nyelv „ujranyitva” változó: a jegyet lezárás után újranyitották.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return any(c.field == "state" and c.old in CLOSED_STATES and c.new and c.new not in CLOSED_STATES
               for c in u.timeline.changes)

def _var_jegy_lezarva(u: UnitContext):
    """Az almérés-nyelv „jegy_lezarva” változó: a jegy most lezárt állapotban van.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.timeline.is_closed

def _var_elozo_csoport(u: UnitContext):
    """A csoport, amelyik ráadta a jegyet (az előző szakasz csoportja); a nyitó csoportnál és ticketnél üres.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), variables._var_elozo_szint."""
    if u.stint is None or u.stint.index == 0:
        return ""
    return u.timeline.stints[u.stint.index - 1].group

def _var_elozo_szint(u: UnitContext):
    """Az almérés-nyelv „elozo_szint” változó: a ráadó csoport szintje a tudástárból, pl. elozo_szint == "L1".

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.levels.get(_var_elozo_csoport(u), "")

def _var_nyito_szint(u: UnitContext):
    """Az almérés-nyelv „nyito_szint” változó: a nyitó csoport szintje a tudástárból.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.levels.get(_var_nyito_csoport(u), "")

# Munkaidő: a csoport ismert (tanult vagy kézi) munkaideje; ha nincs, a hivatali idő (hétköznap 8–17).
# Az időbélyegek az adat szerintiek (a kivonat időzónája nem ellenőrzött).
DEFAULT_SCHEDULE = Schedule(frozenset(range(5)), 8, 17, "hivatali idő")

def _schedule_of(u: UnitContext, group: str) -> Schedule:
    """A csoport munkaideje: a saját (tanult vagy kézi), különben a cégszintű, különben a hivatali idő.

    Hívja: variables._time_variables, variables._var_felvetel_munkaperc, variables._var_szakasz_munkaperc."""
    return u.schedules.get(group) or DEFAULT_SCHEDULE

def _in_working_time(schedule: Schedule, moment: datetime) -> bool:
    """Egy pillanat a munkaidőbe esik-e.

    Hívja: variables._time_variables."""
    return schedule.working_seconds(moment, moment + timedelta(seconds=1)) > 0

def _moment_nyitas(u: UnitContext):
    """A jegy nyitásának pillanata és a nyitó csoport (az időváltozókhoz).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.timeline.opened_at, _var_nyito_csoport(u)

def _moment_kapta(u: UnitContext):
    """A pillanat, amikor a csoport megkapta a jegyet (a szakasz kezdete), és a csoport.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.start, _var_csoport(u)

def _moment_felvette(u: UnitContext):
    """A névre vétel pillanata (vagy None) és a csoport.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    pickup = _pickup(u)
    return (pickup.at if pickup else None), _var_csoport(u)

def _moment_vege(u: UnitContext):
    """A szakasz vége (lezárás vagy továbbítás), ticketnél az első lezárás; még tartó esetnél üres.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _terminal_moment(u), _var_csoport(u)

_MOMENTS = {
    "nyitas": (_moment_nyitas, "a jegy nyitásakor", "a nyitó csoport"),
    "kapta": (_moment_kapta, "amikor a csoport megkapta (a szakasz kezdete; ticketnél a nyitás)", "a csoport"),
    "felvette": (_moment_felvette, "a névre vételkor (üres, ha nem vették névre)", "a csoport"),
    "vege": (_moment_vege, "az eset végén: lezárás vagy továbbítás (üres, ha még tart)", "a csoport"),
}

def _time_variables() -> Dict[str, Tuple[Callable[[UnitContext], Any], str]]:
    """Az időváltozók (nyitas_/kapta_/felvette_/vege_ × ora, nap, hetvege, munkaidoben) előállítása a VARIABLES
    táblához.

    Hívja: children (modulszint)."""
    result: Dict[str, Tuple[Callable[[UnitContext], Any], str]] = {}
    for prefix, (moment_of, when, whose) in _MOMENTS.items():
        def ora(u, m=moment_of):
            at = m(u)[0]
            return at.hour if at else None
        def nap(u, m=moment_of):
            at = m(u)[0]
            return WEEKDAY_LABELS[at.weekday()] if at else ""
        def hetvege(u, m=moment_of):
            at = m(u)[0]
            return at.weekday() >= 5 if at else None
        def munkaidoben(u, m=moment_of):
            at, group = m(u)
            return _in_working_time(_schedule_of(u, group), at) if at else None
        result[f"{prefix}_ora"] = (ora, f"az óra (0–23) {when}")
        result[f"{prefix}_nap"] = (nap, f'a nap {when}, pl. "hétfő", "szombat"')
        result[f"{prefix}_hetvege"] = (hetvege, f"hétvégén volt-e {when}")
        result[f"{prefix}_munkaidoben"] = (munkaidoben, f"{whose} munkaidejében volt-e {when} (ismeretlen munkaidőnél hétköznap 8–17)")
    return result

def _var_felvetel_munkaperc(u: UnitContext):
    """Az almérés-nyelv „felvetel_munkaperc” változó: a névre vételig eltelt munkaidő percben (a csoport munkaideje
    szerint).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    pickup = _pickup(u)
    if pickup is None:
        return None
    return _minutes(_schedule_of(u, _var_csoport(u)).working_hours(u.start, pickup.at))

def _var_szakasz_munkaperc(u: UnitContext):
    """Az almérés-nyelv „szakasz_munkaperc” változó: az eset hossza munkaidőben, percben.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _minutes(_schedule_of(u, _var_csoport(u)).working_hours(u.start, u.end))

def _var_munkarend_ismert(u: UnitContext):
    """Az almérés-nyelv „munkarend_ismert” változó: a csoport munkaideje ismert (különben a hivatali idővel számol).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _var_csoport(u) in u.schedules

# Érdemi munka (interakció): kommunikáció, leírás, hold-indoklás, besorolás javítása. Nem az: a rendszer
# által írt mezők (email, reakcióidő, számlálók), a felelős/állapot/csoport váltása, és ami a lezárás vagy
# a továbbítás mentésével együtt íródik (close notes, resolved_at, routing – az a lezárás/továbbítás része).
INTERACTION_FIELDS = frozenset({
    "work_notes", "comments", "description", "short_description", "u_pending_reason", "u_pending_end_date",
    "u_pending_end_date_new", "relation", "child_incidents", "u_justification", "subcategory", "category",
    "business_service", "impact", "urgency", "priority", "location", "u_specific_service_type", "cmdb_ci",
})

def _terminal_moment(u: UnitContext) -> Optional[datetime]:
    """A szakasz lezáró mentésének ideje (lezárás vagy továbbítás); még tartó szakasznál nincs.

    Hívja: variables._interactions, variables._moment_vege."""
    if u.stint is not None:
        return None if u.stint.is_open else u.stint.end
    return u.timeline.first_closed_at

def _interactions(u: UnitContext, after: Optional[datetime] = None) -> List[Any]:
    """Az eset érdemi lépései (munkajegyzet, komment, besorolás…), a lezáró vagy továbbító mentés előtt; opcionálisan
    csak egy pillanat után.

    Hívja: variables._var_interakciok, variables._var_interakciok_felvetel_utan."""
    terminal = _terminal_moment(u)
    return [c for c in u.own_changes()
            if c.field in INTERACTION_FIELDS and c.new != c.old
            and (after is None or c.at >= after) and (terminal is None or c.at < terminal)]

def _pickup(u: UnitContext):
    """A névre vétel: szakasznál a szakasz felvétele (a továbbítás mentésében kapott felelőssel együtt,
    ugyanúgy, mint a felvételi idő mérése), ticketnél az első felelős.

    Hívja: variables._moment_felvette, variables._var_elso_lepes_felvetel_utan, variables._var_felvetel_munkaperc,
    variables._var_felvetel_perc, variables._var_felvetel_utan_perc, variables._var_interakciok_felvetel_utan."""
    if u.stint is not None:
        return u.stint.first_assignment
    return next((c for c in u.timeline.changes if c.field == "assigned_to" and c.new), None)

IN_PROGRESS_STATE = "2"   # "folyamatban": a névre vétel rutinszerű kísérője, nem érdemi állapotváltás

def _kind(change: Any) -> Optional[str]:
    """Egy változás fajtája a „felvétel utáni első lépés” változóhoz: lezaras, allapot, atadas, interakcio vagy semmi.

    Hívja: variables._var_elso_lepes_felvetel_utan."""
    if change.field == "state":
        if change.new == IN_PROGRESS_STATE:
            return None
        return "lezaras" if change.new in CLOSED_STATES else "allapot"
    if change.field == "assigned_to":
        return "atadas"
    if change.field in INTERACTION_FIELDS:
        return "interakcio"
    return None   # rendszerzaj vagy a lezáró mentés kísérő mezői

_TERMINAL_ORDER = ("lezaras", "tovabbitas", "allapot", "interakcio", "atadas")

def _var_elso_lepes_felvetel_utan(u: UnitContext) -> str:
    """Mi volt a felvétel (névre vétel) utáni első lépés: interakcio | lezaras | tovabbitas | allapot |
    atadas (másik emberre) | nincs (azóta semmi); felvétel nélkül üres. Egy mentésen belül a lezárás és a
    továbbítás erősebb (a vele együtt írt megjegyzés a lezárás része).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    pickup = _pickup(u)
    if pickup is None:
        return ""
    moments: Dict[datetime, set] = {}
    for change in u.own_changes():
        if change.at < pickup.at or change is pickup:
            continue
        kind = _kind(change)
        if kind is not None and not (kind == "atadas" and change.at == pickup.at):
            moments.setdefault(change.at, set()).add(kind)
    if u.stint is not None and u.stint.end_reason == "transfer":
        moments.setdefault(u.stint.end, set()).add("tovabbitas")
    if not moments:
        return "nincs"
    first = moments[min(moments)]
    return next(kind for kind in _TERMINAL_ORDER if kind in first)

def _var_felvetel_utan_perc(u: UnitContext):
    """Az almérés-nyelv „felvetel_utan_perc” változó: a névre vételtől az eset végéig eltelt percek (üres, ha nem
    vették névre).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    pickup = _pickup(u)
    return _minutes((u.end - pickup.at).total_seconds() / 3600) if pickup else None

def _var_interakciok(u: UnitContext):
    """Az almérés-nyelv „interakciok” változó: érdemi lépések száma (munkajegyzet, komment, leírás, hold-indoklás,
    besorolás); a lezáró/továbbító mentés és a rendszermezők nem számítanak.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return len(_interactions(u))

def _var_interakciok_felvetel_utan(u: UnitContext):
    """Az almérés-nyelv „interakciok_felvetel_utan” változó: érdemi lépések száma a névre vétel után.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    pickup = _pickup(u)
    return len(_interactions(u, pickup.at)) if pickup else 0

def _var_elutasitva(u: UnitContext):
    """Az almérés-nyelv „elutasitva” változó: elutasítva (u_rejected = 1 az eset végén).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.timeline.value_at("u_rejected", u.end) in ("1", "true")

VARIABLES: Dict[str, Tuple[Callable[[UnitContext], Any], str]] = {
    "szakasz_perc": (_var_szakasz_perc, "az eset hossza percben (szakasznál a csoportnál töltött idő, ticketnél a lezárásig)"),
    "jegy_perc": (_var_jegy_perc, "a teljes jegy élete percben (nyitástól az első lezárásig vagy máig)"),
    "felvetel_perc": (_var_felvetel_perc, "hány perc múlva lett felelőse (üres, ha nem lett)"),
    "hold_perc": (_var_hold_perc, "hold-ban töltött percek"),
    "lezarta": (_var_lezarta, "a csoport (szakasznál) lezárta a jegyet / a jegy le lett zárva"),
    "tovabbitotta": (_var_tovabbitotta, "a szakasz továbbítással ért véget"),
    "nyitott": (_var_nyitott, "még tart"),
    "csoport": (_var_csoport, "a csoport (szakasznál az övé)"),
    "kovetkezo_csoport": (_var_kovetkezo_csoport, "továbbításnál a következő csoport"),
    "szint": (_var_szint, "a csoport szintje a tudástárból (pl. L1)"),
    "allapot": (_var_allapot, "az állapot (state) az eset végén, pl. '6' megoldott, '7' lezárt"),
    "nyito_csoport": (_var_nyito_csoport, "a jegyet elsőként kapó csoport"),
    "zaro_csoport": (_var_zaro_csoport, "a jegyet lezáró csoport (üres, ha nincs lezárva)"),
    "csoportok_szama": (_var_csoportok_szama, "hány csoport érintette a jegyet"),
    "egy_csoport": (_var_egy_csoport, "csak egy csoport érintette a jegyet"),
    "nyit_zar_egyedul": (_var_nyit_zar_egyedul, "egyetlen csoport kapta és zárta le (nem került máshoz)"),
    "ujrakiosztasok": (_var_ujrakiosztasok, "csoportváltások száma (az L1-ből indulás nem számít)"),
    "ujranyitva": (_var_ujranyitva, "a jegyet lezárás után újranyitották"),
    "jegy_lezarva": (_var_jegy_lezarva, "a jegy most lezárt állapotban van"),
    "felvetel_utan_perc": (_var_felvetel_utan_perc, "a névre vételtől az eset végéig eltelt percek (üres, ha nem vették névre)"),
    "interakciok": (_var_interakciok, "érdemi lépések száma (munkajegyzet, komment, leírás, hold-indoklás, besorolás); "
                                      "a lezáró/továbbító mentés és a rendszermezők nem számítanak"),
    "interakciok_felvetel_utan": (_var_interakciok_felvetel_utan, "érdemi lépések száma a névre vétel után"),
    "elso_lepes_felvetel_utan": (_var_elso_lepes_felvetel_utan,
                                 'a névre vétel utáni első lépés: "interakcio", "lezaras", "tovabbitas", "allapot" (pl. hold; '
                                 'a "folyamatban"-ra állítás nem lépés), "atadas" (másik emberre), "nincs"; névre vétel nélkül üres'),
    "elutasitva": (_var_elutasitva, "elutasítva (u_rejected = 1 az eset végén)"),
    "elozo_csoport": (_var_elozo_csoport, "a csoport, amelyik ráadta a jegyet (az előző szakaszé; a nyitó csoportnál üres)"),
    "elozo_szint": (_var_elozo_szint, 'a ráadó csoport szintje a tudástárból, pl. elozo_szint == "L1"'),
    "nyito_szint": (_var_nyito_szint, "a nyitó csoport szintje a tudástárból"),
    **_time_variables(),
    "felvetel_munkaperc": (_var_felvetel_munkaperc, "a névre vételig eltelt munkaidő percben (a csoport munkaideje szerint)"),
    "szakasz_munkaperc": (_var_szakasz_munkaperc, "az eset hossza munkaidőben, percben"),
    "munkarend_ismert": (_var_munkarend_ismert, "a csoport munkaideje ismert (különben a hivatali idővel számol)"),
}
