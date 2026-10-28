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

import ast
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from models.discovery.mining import with_implied
from models.knowledge_base import CHILD_DIMENSION_PREFIX
from models.timeline import CLOSED_STATES, Stint, TicketTimeline, canonical_field
from models.working_time import WEEKDAY_LABELS, Schedule

ROLES = {"explain": "magyarázat", "alert": "riasztás"}
MIN_UNITS = 3            # ennél kevesebb érintett esetből nem mondunk ítéletet
MAX_UNITS = 2000         # egy oldalon legfeljebb ennyi esetet értékelünk ki (determinisztikus ritkítással)

UnitRef = Tuple[str, Optional[int]]   # (incidens, szakasz sorszáma; ticket egységnél None)


# --------------------------------------------------------------------------
# Szókincs
# --------------------------------------------------------------------------

def _minutes(hours: float) -> float:
    """Órából percet számol (két tizedesre kerekítve).

    Hívja: children._var_felvetel_munkaperc, children._var_felvetel_perc, children._var_felvetel_utan_perc,
    children._var_hold_perc, children._var_jegy_perc, children._var_szakasz_munkaperc, children._var_szakasz_perc."""
    return round(hours * 60, 2)


class UnitContext:
    """Egy eset (szakasz vagy ticket) a kifejezés szemszögéből."""

    def __init__(self, timeline: TicketTimeline, stint: Optional[Stint], levels: Mapping[str, str],
                 schedules: Optional[Mapping[str, Schedule]] = None):
        """Egy eset (ticket, vagy szakasz: egy csoportnál töltött idő) a kifejezés-értelmező számára: az idővonal, a
        szakasz, a csoportszintek és a munkaidők."""
        self.timeline, self.stint, self.levels = timeline, stint, levels
        self.schedules = schedules or {}

    @property
    def end(self) -> datetime:
        """Az eset vége: a szakasz vége, ticketnél az első lezárás vagy a mérés pillanata."""
        if self.stint is not None:
            return self.stint.end
        return self.timeline.first_closed_at or self.timeline.as_of

    @property
    def start(self) -> datetime:
        """Az eset kezdete: a szakasz kezdete, ticketnél a nyitás."""
        return self.stint.start if self.stint is not None else self.timeline.opened_at

    def own_changes(self):
        """Az eset saját változásai: szakasznál a csoport által tett mentések, ticketnél mind.

        Hívja: children._fn_kitoltve, children._fn_valtozasok, children._interactions,
        children._var_elso_lepes_felvetel_utan."""
        if self.stint is None:
            return list(self.timeline.changes)
        return self.timeline.own_changes(self.stint)

    def _closing_stint(self) -> Optional[Stint]:
        """A jegyet lezáró szakasz (vagy None, ha nincs lezárva).

        Hívja: children._var_zaro_csoport."""
        return next((s for s in reversed(self.timeline.stints) if s.end_reason == "closed"), None)


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

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), children._moment_felvette,
    children._moment_kapta, children._moment_vege, children._var_felvetel_munkaperc, children._var_munkarend_ismert,
    children._var_szakasz_munkaperc, children._var_szint."""
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

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), children._moment_nyitas,
    children._var_nyito_szint."""
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

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), children._var_elozo_szint."""
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

    Hívja: children._time_variables, children._var_felvetel_munkaperc, children._var_szakasz_munkaperc."""
    return u.schedules.get(group) or DEFAULT_SCHEDULE


def _in_working_time(schedule: Schedule, moment: datetime) -> bool:
    """Egy pillanat a munkaidőbe esik-e.

    Hívja: children._time_variables."""
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

    Hívja: children._interactions, children._moment_vege."""
    if u.stint is not None:
        return None if u.stint.is_open else u.stint.end
    return u.timeline.first_closed_at


def _interactions(u: UnitContext, after: Optional[datetime] = None) -> List[Any]:
    """Az eset érdemi lépései (munkajegyzet, komment, besorolás…), a lezáró vagy továbbító mentés előtt; opcionálisan
    csak egy pillanat után.

    Hívja: children._var_interakciok, children._var_interakciok_felvetel_utan."""
    terminal = _terminal_moment(u)
    return [c for c in u.own_changes()
            if c.field in INTERACTION_FIELDS and c.new != c.old
            and (after is None or c.at >= after) and (terminal is None or c.at < terminal)]


def _pickup(u: UnitContext):
    """A névre vétel: szakasznál a szakasz felvétele (a továbbítás mentésében kapott felelőssel együtt,
    ugyanúgy, mint a felvételi idő mérése), ticketnél az első felelős.

    Hívja: children._moment_felvette, children._var_elso_lepes_felvetel_utan, children._var_felvetel_munkaperc,
    children._var_felvetel_perc, children._var_felvetel_utan_perc, children._var_interakciok_felvetel_utan."""
    if u.stint is not None:
        return u.stint.first_assignment
    return next((c for c in u.timeline.changes if c.field == "assigned_to" and c.new), None)


IN_PROGRESS_STATE = "2"   # "folyamatban": a névre vétel rutinszerű kísérője, nem érdemi állapotváltás


def _kind(change: Any) -> Optional[str]:
    """Egy változás fajtája a „felvétel utáni első lépés” változóhoz: lezaras, allapot, atadas, interakcio vagy semmi.

    Hívja: children._var_elso_lepes_felvetel_utan."""
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


def _fn_ertek(u: UnitContext, name: Any) -> str:
    """Az almérés-nyelv „ertek” függvény: ertek("mező"): a mező értéke az eset végén.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla), children._fn_ures."""
    return u.timeline.value_at(canonical_field(name), u.end)


def _fn_ertek_kezdetkor(u: UnitContext, name: Any) -> str:
    """Az almérés-nyelv „ertek_kezdetkor” függvény: ertek_kezdetkor("mező"): a mező értéke az eset kezdetén.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return u.timeline.value_at(canonical_field(name), u.start)


def _fn_kitoltve(u: UnitContext, name: Any) -> bool:
    """Az almérés-nyelv „kitoltve” függvény: kitoltve("mező"): az eset alatt (szakasznál a csoport maga) kitöltötte a
    mezőt.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    key = canonical_field(name)
    return any(c.field == key and c.new for c in u.own_changes())


def _fn_valtozasok(u: UnitContext, name: Any) -> int:
    """Az almérés-nyelv „valtozasok” függvény: valtozasok("mező"): hányszor változott a mező az eset alatt.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    key = canonical_field(name)
    return sum(1 for c in u.own_changes() if c.field == key)


def _fn_ures(u: UnitContext, name: Any) -> bool:
    """Az almérés-nyelv „ures” függvény: ures("mező"): a mező üres az eset végén.

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _fn_ertek(u, name) == ""


def _fn_szam(u: UnitContext, value: Any) -> Optional[float]:
    """Az almérés-nyelv „szam” függvény: szam(x): számmá alakít (pl. szam(ertek("reopen_count")) > 0).

    Hívja: az almérés-nyelv értelmezője (VARIABLES / FUNCTIONS tábla)."""
    return _number(value)


FUNCTIONS: Dict[str, Tuple[Callable[..., Any], str]] = {
    "ertek": (_fn_ertek, 'ertek("mező"): a mező értéke az eset végén'),
    "ertek_kezdetkor": (_fn_ertek_kezdetkor, 'ertek_kezdetkor("mező"): a mező értéke az eset kezdetén'),
    "kitoltve": (_fn_kitoltve, 'kitoltve("mező"): az eset alatt (szakasznál a csoport maga) kitöltötte a mezőt'),
    "valtozasok": (_fn_valtozasok, 'valtozasok("mező"): hányszor változott a mező az eset alatt'),
    "ures": (_fn_ures, 'ures("mező"): a mező üres az eset végén'),
    "szam": (_fn_szam, 'szam(x): számmá alakít (pl. szam(ertek("reopen_count")) > 0)'),
}


FIELD_FUNCTIONS = frozenset({"ertek", "ertek_kezdetkor", "kitoltve", "valtozasok", "ures"})   # mezőnevet várnak


def vocabulary() -> Dict[str, Any]:
    """A szókincs a felületnek (súgó).

    Hívja: DiscoveryService.children_overview."""
    return {
        "variables": [{"name": k, "description": d} for k, (_, d) in VARIABLES.items()],
        "functions": [{"name": k, "description": d} for k, (_, d) in FUNCTIONS.items()],
        "operators": "and, or, not, <, <=, >, >=, ==, !=, in, not in, +, -, *, /; szöveg idézőjelben, lista [..]",
        "example": 'szakasz_perc < 15 and lezarta and kitoltve("close_notes")',
    }


# --------------------------------------------------------------------------
# Nyelv: ellenőrzés és kiértékelés (saját értelmező, nincs eval)
# --------------------------------------------------------------------------

_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
            ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.In, ast.NotIn, ast.Name, ast.Load,
            ast.Constant, ast.Call, ast.List, ast.Tuple, ast.BinOp, ast.Add, ast.Sub, ast.Mult, ast.Div)


def compile_expression(source: str) -> ast.Expression:
    """A kifejezés ellenőrzése; hibánál ValueError érthető üzenettel.

    Hívja: DiscoveryManager.add_child, DiscoveryService.children_action, children._run_group,
    children.derived_evaluator, children.rows_to_expression, children.run_child."""
    text = str(source or "").strip()
    if not text:
        raise ValueError("Üres kifejezés.")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"Nem értelmezhető kifejezés ({error.msg}, {error.offset}. karakter).") from None
    # a függvények mezőnév-argumentuma idézőjel nélkül is jó: valtozasok(assignment_group)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FIELD_FUNCTIONS:
            node.args = [ast.copy_location(ast.Constant(arg.id), arg)
                         if isinstance(arg, ast.Name) and arg.id not in VARIABLES else arg for arg in node.args]
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ValueError(f"Nem megengedett elem a kifejezésben: {type(node).__name__}.")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                name = node.func.id if isinstance(node.func, ast.Name) else "?"
                raise ValueError(f"Ismeretlen függvény: {name} (lehetséges: {', '.join(FUNCTIONS)}).")
        elif isinstance(node, ast.Name) and node.id not in VARIABLES and node.id not in FUNCTIONS \
                and node.id not in ("True", "False"):
            raise ValueError(f"Ismeretlen név: {node.id} (lehetséges: {', '.join(VARIABLES)}).")
        elif isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, float, bool)) and node.value is not None:
            raise ValueError("Csak szöveg, szám és igaz/hamis érték adható meg.")
    return tree


def _number(value: Any) -> Optional[float]:
    """Számmá alakít, ha lehet (különben None).

    Hívja: children._compare, children._evaluate, children._fn_szam."""
    if isinstance(value, bool) or value is None:
        return None if value is None else float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def _compare(op: ast.cmpop, left: Any, right: Any) -> bool:
    """Két érték összehasonlítása egy relációval (számként, ha mindkettő az; különben szövegként).

    Hívja: children._evaluate."""
    if isinstance(op, (ast.In, ast.NotIn)):
        container = right if isinstance(right, (list, tuple)) else [right]
        values = {str(v) for v in container}
        found = str(left) in values or (_number(left) is not None and _number(left) in {_number(v) for v in container})
        return found if isinstance(op, ast.In) else not found
    if isinstance(op, (ast.Eq, ast.NotEq)):
        same = left == right or (str(left) == str(right) and left is not None and right is not None)
        if not same and _number(left) is not None and _number(right) is not None:
            same = _number(left) == _number(right)
        return same if isinstance(op, ast.Eq) else not same
    a, b = _number(left), _number(right)
    if a is None or b is None:
        return False   # hiányzó érték (pl. nem lett felelős) nem kisebb és nem nagyobb semminél
    return {ast.Lt: a < b, ast.LtE: a <= b, ast.Gt: a > b, ast.GtE: a >= b}[type(op)]


def _evaluate(node: ast.AST, unit: UnitContext) -> Any:
    """A kifejezésfa egy csomópontjának kiértékelése egy eseten (saját, biztonságos értelmező: csak a megengedett
    műveletek, változók és függvények).

    Hívja: children.evaluate, children.evaluate_value."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body, unit)
    if isinstance(node, ast.BoolOp):
        values = (bool(_evaluate(v, unit)) for v in node.values)
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.UnaryOp):
        value = _evaluate(node.operand, unit)
        if isinstance(node.op, ast.Not):
            return not value
        number = _number(value)
        return None if number is None else -number
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, unit)
        for op, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, unit)
            if not _compare(op, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BinOp):
        a, b = _number(_evaluate(node.left, unit)), _number(_evaluate(node.right, unit))
        if a is None or b is None or (isinstance(node.op, ast.Div) and b == 0):
            return None
        return {ast.Add: a + b, ast.Sub: a - b, ast.Mult: a * b, ast.Div: a / b if b else None}[type(node.op)]
    if isinstance(node, ast.Name):
        if node.id in ("True", "False"):
            return node.id == "True"
        return VARIABLES[node.id][0](unit)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_evaluate(e, unit) for e in node.elts]
    if isinstance(node, ast.Call):
        return FUNCTIONS[node.func.id][0](unit, *[_evaluate(a, unit) for a in node.args])
    raise ValueError(f"Nem megengedett elem: {type(node).__name__}")


def evaluate(tree: ast.Expression, unit: UnitContext) -> bool:
    """Egy lefordított kifejezés igaz-e az esetre (ellenőrző almérés, keresési szempont).

    Hívja: children.derived_evaluator, children.measure_units."""
    return bool(_evaluate(tree, unit))


def evaluate_value(tree: ast.Expression, unit: UnitContext) -> str:
    """Csoportosításhoz: a kifejezés értéke szövegként (üres érték: "").

    Hívja: children.derived_evaluator, children.measure_groups."""
    value = _evaluate(tree, unit)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "igen" if value else "nem"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


METHODS = {"check": "ellenőrzés", "group": "csoportosítás"}
GROUP_ROWS = 5


_ROW_OPS = {"<", "<=", ">", ">=", "==", "!=", "in", "not in"}


def rows_to_expression(rows: Sequence[Any]) -> str:
    """Adatsor -> kifejezés. Elemei: feltétel ({"field"|"var"|"fn"+"arg", "op", "value", "not"}) vagy
    "and" / "or" kapcsoló (két feltétel között; alapból "and").

    Hívja: DiscoveryService.children_action."""
    parts: List[str] = []
    for row in rows:
        if isinstance(row, str):
            if row.strip().lower() not in ("and", "or"):
                raise ValueError(f"Ismeretlen kapcsoló: {row} (and | or).")
            parts.append(row.strip().lower())
            continue
        if parts and parts[-1] not in ("and", "or"):
            parts.append("and")
        if "field" in row:
            subject = f"ertek({row['field']!r})"
        elif "fn" in row:
            subject = f"{row['fn']}({row.get('arg', '')!r})"
        else:
            subject = str(row.get("var", ""))
        op = str(row.get("op", "")).strip()
        if op:
            if op not in _ROW_OPS:
                raise ValueError(f"Ismeretlen művelet: {op}.")
            value = row.get("value")
            text = f"{subject} {op} {value!r}"
        else:
            text = subject
        parts.append(f"not ({text})" if row.get("not") else f"({text})")
    expression = " ".join(parts)
    compile_expression(expression)
    return expression


# --------------------------------------------------------------------------
# Definíció, kiváltó és kiértékelés
# --------------------------------------------------------------------------

@dataclass
class ChildDef:
    child_id: str
    name: str
    expression: str
    role: str = "explain"
    threshold: float = 0.6                  # az érintett esetek ekkora hányadánál "teljesül"
    trigger: Dict[str, Any] = field(default_factory=dict)   # candidate_id | kind | measure | items
    note: str = ""
    state: str = "active"
    created_at: str = ""
    source: str = ""                        # melyik jelöltnél tanította az elemző
    stats: Dict[str, int] = field(default_factory=lambda: {"runs": 0, "triggered": 0, "decided": 0, "agreed": 0})
    as_dimension: bool = False              # keresési szempont: igen/nem dimenzió az adatlapokon
    reviewed_at: str = ""                   # az elemző megerősítette (a felülvizsgálat ettől számol)
    reviewed_stats: Dict[str, int] = field(default_factory=dict)   # a statisztika a megerősítéskor
    triggers: List[Dict[str, Any]] = field(default_factory=list)   # további kiváltók (meglévő almérés hozzárendelése)
    auto: bool = False                      # magyarázatnál: ha teljesül, "most nem" döntés az elemző helyett
    excluded: List[Dict[str, Any]] = field(default_factory=list)   # "nem jó módszer" ezeknél a jelöltfajtáknál
    method: str = "check"                   # check: igaz/hamis állítás | group: csoportosítás egy érték szerint

    def all_triggers(self) -> List[Dict[str, Any]]:
        """Az almérés összes kiváltója (az eredeti és a később hozzárendeltek).

        Hívja: ChildDef.attach, ChildDef.matches, ChildDef.triggers_label."""
        return [self.trigger] + list(self.triggers)

    def matches(self, candidate: Any) -> bool:
        """Lefut-e az almérés erre a jelöltre: valamelyik kiváltója illik, és a „nem jó” kizárások egyike sem.

        Hívja: DiscoveryService.children_action."""
        if any(trigger_matches(trigger, candidate) for trigger in self.excluded):
            return False
        return any(trigger_matches(trigger, candidate) for trigger in self.all_triggers())

    @property
    def triggers_label(self) -> str:
        """A kiváltók szavakban (a Tudás fül és a hatásablak szövegéhez)."""
        return "; ".join(trigger_label(trigger) for trigger in self.all_triggers())

    def attach(self, trigger: Mapping[str, Any]) -> bool:
        """Újabb kiváltó; False, ha már pontosan ez megvan.

        Hívja: DiscoveryManager.attach_child, DiscoveryManager.child_method_feedback."""
        trigger = dict(trigger)
        if any(_same_trigger(trigger, existing) for existing in self.all_triggers()):
            return False
        self.triggers.append(trigger)
        return True

    @property
    def dimension(self) -> str:
        """A keresési szempontként használt almérés dimenzióneve (almeres:CHD-…)."""
        return CHILD_DIMENSION_PREFIX + self.child_id

    def stats_since_review(self) -> Dict[str, int]:
        """A futási és egyezési statisztika az utolsó megerősítés óta (a felülvizsgálathoz).

        Hívja: children.review_children."""
        return {k: self.stats.get(k, 0) - self.reviewed_stats.get(k, 0) for k in ("runs", "triggered", "decided", "agreed")}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ChildDef":
        """Az almérés a tárolt szótárból.

        Hívja: DiscoveryManager._child, DiscoveryManager.children, DiscoveryManager.revoke_child."""
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    def to_dict(self) -> Dict[str, Any]:
        """Az almérés tárolható szótárként.

        Hívja: DiscoveryManager._save_child, DiscoveryManager.add_child, DiscoveryManager.revoke_child,
        DiscoveryService.children_action."""
        return asdict(self)


def _same_trigger(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Két kiváltó ugyanaz-e (a sorrendtől függetlenül).

    Hívja: ChildDef.attach."""
    normalize = lambda t: {k: (sorted(map(tuple, v)) if k == "items" else v) for k, v in t.items() if v}
    return normalize(a) == normalize(b)


def trigger_label(trigger: Mapping[str, Any]) -> str:
    """Egy kiváltó szavakban (pl. „jelzéseknél, ahol feature=work_notes”).

    Hívja: ChildDef.triggers_label, children.trigger_options."""
    parts = []
    if trigger.get("candidate_id"):
        parts.append(f"csak a(z) {trigger['candidate_id']} jelöltnél")
    if trigger.get("kind"):
        parts.append({"signal": "jelzéseknél", "contrast": "kiugró mintáknál", "trend": "romlásoknál"}.get(trigger["kind"], trigger["kind"]))
    if trigger.get("measure"):
        parts.append(f"a(z) {trigger['measure']} mérésnél")
    if trigger.get("items"):
        parts.append("ahol " + " és ".join(f"{d}={v}" for d, v in trigger["items"]))
    return ", ".join(parts) or "minden jelöltnél"


def trigger_matches(trigger: Mapping[str, Any], candidate: Any) -> bool:
    """Egy kiváltó illik-e a jelöltre (azonosító, fajta, mérés, mintaelemek).

    Hívja: ChildDef.matches."""
    if trigger.get("candidate_id") and trigger["candidate_id"] != candidate.candidate_id:
        return False
    if trigger.get("kind") and trigger["kind"] != candidate.kind:
        return False
    if trigger.get("measure") and trigger["measure"] != candidate.measure:
        return False
    items = {(str(d), str(v)) for d, v in trigger.get("items") or ()}
    if items and not any(items <= set(with_implied(tuple(sorted(desc)))) for desc in candidate.descriptions()):
        return False
    return True


def trigger_options(candidate: Any) -> List[Dict[str, Any]]:
    """Mennyire általánosítsuk a tanítást (a kártya választólistájához), a legszűkebbtől.

    Hívja: DiscoveryService._refinements, DiscoveryService.candidate_dto."""
    options = [{"candidate_id": candidate.candidate_id}]
    for item in candidate.items:
        options.append({"kind": candidate.kind, "items": [list(item)]})
    if len(candidate.items) > 1:
        options.append({"kind": candidate.kind, "items": [list(i) for i in candidate.items]})
    options.append({"kind": candidate.kind, "measure": candidate.measure})
    options.append({})
    unique, seen = [], set()
    for option in options:
        key = repr(sorted((k, repr(v)) for k, v in option.items()))
        if key not in seen:
            seen.add(key)
            unique.append({"trigger": option, "label": trigger_label(option)})
    return unique


def _thin(units: Sequence[UnitRef]) -> List[UnitRef]:
    """Legfeljebb MAX_UNITS eset (determinisztikus ritkítással), hogy egy almérés nagy adaton se legyen lassú.

    Hívja: children.measure_groups, children.measure_units."""
    units = sorted(set(units), key=lambda u: (u[0], -1 if u[1] is None else u[1]))
    if len(units) <= MAX_UNITS:
        return units
    step = len(units) / MAX_UNITS
    return [units[int(i * step)] for i in range(MAX_UNITS)]


def measure_units(tree: ast.Expression, units: Sequence[UnitRef], timeline_of: Callable[[str], Optional[TicketTimeline]],
                  levels: Mapping[str, str], schedules: Optional[Mapping[str, Schedule]] = None) -> Tuple[int, int, List[str]]:
    """(kiértékelt esetek, ahol igaz, néhány incidens ahol igaz).

    Hívja: children.run_child."""
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

    Hívja: children._run_group."""
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

    Hívja: children.run_child."""
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

    Hívja: DiscoveryService._run_children, DiscoveryService.children_action, children.run_probes."""
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


def describe_result(name: str, role: str, n: int, hits: int, rn: int, rhits: int, triggered: bool) -> str:
    """Egy ellenőrző almérés eredménye szavakban (arány az érintetteknél és a többinél, ítélet).

    Hívja: children.run_child, children.run_probes."""
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

    Hívja: profile.build_profiles."""
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


@dataclass(frozen=True)
class ChildReviewParams:
    min_runs: int = 10            # ennyi futás után jelez, ha egyszer sem teljesült
    min_decisions: int = 5        # ennyi döntés kell az egyezés megítéléséhez
    min_agreement: float = 0.5    # ennél ritkább egyezés: a jelzése félrevisz
    min_age_days: int = 14        # ennyi idős, egyszer sem futott almérés jelez


def review_children(children: Iterable[ChildDef], now: datetime, params: ChildReviewParams = ChildReviewParams()) -> List[Dict[str, Any]]:
    """Felülvizsgálandó almérések: sosem futott, sosem teljesül, vagy a döntések rendre mást mondanak.
    A megerősítés óta gyűlt statisztikából számol (az elemző döntése nem jön vissza ugyanazért).

    Hívja: DiscoveryService.review."""
    items = []
    for child in children:
        if child.state != "active":
            continue
        stats = child.stats_since_review()
        since = child.reviewed_at or child.created_at
        age = (now - datetime.fromisoformat(since)).total_seconds() / 86400 if since else 0.0
        if stats["decided"] >= params.min_decisions and stats["agreed"] / stats["decided"] < params.min_agreement:
            reason, evidence = "disagreed", stats["decided"]
            message = (f"teljesülése után {stats['decided']} döntésből csak {stats['agreed']} egyezett a jelzésével "
                       f"({ROLES[child.role]}) – félrevisz?")
        elif stats["runs"] >= params.min_runs and stats["triggered"] == 0:
            reason, evidence = "never_triggered", stats["runs"]
            message = (f"{stats['runs']} futásból egyszer sem teljesült – túl szigorú a küszöb ({child.threshold:.0%}), "
                       f"vagy nem ott fut, ahol kellene?")
        elif stats["runs"] == 0 and age >= params.min_age_days:
            reason, evidence = "never_ran", 0
            message = f"{age:.0f} napja él, és még egyszer sem futott: a kiváltójára ({child.triggers_label}) nem illett jelölt"
        else:
            continue
        items.append({"kind": "child", "object_id": child.child_id, "reason": reason, "message": message,
                      "evidence": evidence, "rendered": f"{child.name}: {child.expression}"})
    return items


def unit_ref(profile: Any) -> UnitRef:
    """Adatlap -> eset hivatkozás (szakasznál "<incidens>#<sorszám>").

    Hívja: DiscoveryService._unit_sets."""
    unit_id = getattr(profile, "unit_id", "") or ""
    if "#" in unit_id:
        incident_id, _, index = unit_id.rpartition("#")
        return incident_id, int(index)
    return profile.incident_id, None
