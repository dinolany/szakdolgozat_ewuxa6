"""ServiceNow-funkciók használata csoportonként: ki milyen funkciót nem használ (célzott edukációhoz).

Nem az átlaghoz mért eltérés a kérdés, hanem az, hogy egy funkciót, amikor alkalmazható lenne,
egy csoport használ-e egyáltalán. Minden funkció a katalógusban:
- előjel: + (a használat jó jel; a nem használat a gond) vagy − (a bekövetkezés rossz jel);
- alkalom: mikor várható el (pl. a szakaszban hold kezdődött), és kinek tudható be;
- használat: a csoport saját változásai közt (a létrehozáskori alapértékek és a küldő
  továbbítási mentése nem a csoporté, ld. TicketTimeline.own_changes).

A katalógus bővíthető: config/feature_catalog.json (ld. load_catalog).

A funkciók gyakran nem alapértelmezettek: nem minden csoport űrlapján vannak rajta. Ezért a
"sosem használja" csak akkor edukációs jelzés, ha a funkció a csoportnak elérhető (használta
már, vagy tény rögzíti); ha ez ismeretlen, a jelzés kérdés az elemzőnek, a válasza tényként rögzül
(elerheto / nem_elerheto), így a funkciók feltérképezése a diagnosztikával együtt épül.
"""

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from config.paths import CONFIG_DIR
from model.diagnostics.core import (
    LOOKBACK_KEY,
    CrossTicketDiagnosis,
    MeasurementContext,
    MeasurementWindow,
    Observation,
    ParameterDef,
    WindowType,
)
from model.diagnostics.history_refs import make_history_ref
from model.knowledge.knowledge_base import ALL_GROUPS, L1_LEVEL
from model.issue import SeverityLevel
from model.timeline.ticket_timeline import HOLD_STATE, FieldChange, Stint, TicketTimeline, incident_timeline

@dataclass(frozen=True)
class Occasion:
    """Egy alkalom, amikor a funkció alkalmazható volt, és hogy élt-e vele a csoport."""

    feature: str
    group: str
    incident_id: str
    at: datetime
    used: bool
    anchor: FieldChange                   # az alkalmat kiváltó esemény (pl. hold kezdete, továbbítás)
    evidence: Tuple[FieldChange, ...]     # a használat eseményei
    stint: int = -1                       # a szakasz sorszáma a ticketben (almérésekhez)


@dataclass(frozen=True)
class FeatureDef:
    """Egy vizsgált funkció. A `kind` választja ki az általános felismerőt (ld. DETECTORS), így új
    mező kódírás nélkül, a katalógusfájlban is felvehető."""

    key: str
    label: str
    polarity: int                         # +1: a használat jó jel; -1: a bekövetkezés rossz jel
    fields: Tuple[str, ...]
    kind: str
    applies: str = ""                     # mikor várható el (megjelenítéshez; üresen a felismerő leírása)
    ignore_values: FrozenSet[str] = frozenset()   # alapértékek, amelyek nem számítanak használatnak
    levels: FrozenSet[str] = frozenset()  # csak ezen szintű csoportokra (üres: mindenkire)
    verb: str = ""                        # a használat szavakban, pl. "ír munkajegyzetet" (a jelzés fejlécéhez)

    @property
    def description(self) -> str:
        return self.applies or DETECTORS[self.kind][0]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "FeatureDef":
        polarity = data.get("polarity", 1)
        if isinstance(polarity, str):
            polarity = -1 if polarity.strip() in ("-", "−") else 1
        kind = data["kind"]
        if kind not in DETECTORS:
            raise ValueError(f"Ismeretlen felismerő: {kind} (lehetséges: {', '.join(DETECTORS)})")
        return cls(
            str(data["key"]), str(data.get("label", data["key"])), int(polarity), tuple(data["fields"]), kind,
            str(data.get("applies", "")), frozenset(str(v).lower() for v in data.get("ignore_values", ())),
            frozenset(data.get("levels", ())), str(data.get("verb", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "label": self.label, "polarity": "+" if self.polarity > 0 else "-",
                "fields": list(self.fields), "kind": self.kind, "applies": self.applies,
                "ignore_values": sorted(self.ignore_values), "levels": sorted(self.levels), "verb": self.verb}


def _meaningful(feature: FeatureDef, changes: Iterable[FieldChange]) -> List[FieldChange]:
    return [c for c in changes if c.field in feature.fields and c.new and c.new.lower() not in feature.ignore_values]


def _hold_starts(timeline: TicketTimeline, stint: Stint) -> List[FieldChange]:
    """A csoport által indított hold-ok (state/incident_state duplikátumok nélkül)."""
    starts, seen = [], set()
    for change in timeline.own_changes(stint):
        if change.field == "state" and change.new == HOLD_STATE and change.old != HOLD_STATE and change.at not in seen:
            seen.add(change.at)
            starts.append(change)
    return starts


def _end_anchor(timeline: TicketTimeline, stint: Stint) -> FieldChange:
    """A szakasz végének eseménye: a továbbítás (a következő szakasz kezdete), a lezárás, vagy ha a
    lezárás nem ebben a szakaszban történt (lezárás utáni továbbítás), a szakasz utolsó eseménye."""
    following = timeline.next_stint(stint)
    if following is not None:
        return following.changes[0]
    closing = next((c for c in reversed(stint.changes) if c.field == "state"), None)
    return closing or stint.changes[-1]


Found = Iterable[Tuple[datetime, bool, FieldChange, Tuple[FieldChange, ...]]]


def _on_hold(feature, timeline, stint, params) -> Found:
    tolerance = timedelta(minutes=params.get("hold_tolerance_minutes", 30))
    own = _meaningful(feature, timeline.own_changes(stint))
    for start in _hold_starts(timeline, stint):
        used = tuple(c for c in own if abs(c.at - start.at) <= tolerance)
        yield start.at, bool(used), start, used


def _on_close(feature, timeline, stint, params) -> Found:
    if stint.end_reason == "closed":
        used = tuple(_meaningful(feature, timeline.own_changes(stint)))
        yield stint.end, bool(used), _end_anchor(timeline, stint), used


def _on_work(feature, timeline, stint, params) -> Found:
    if stint.is_open or (stint.first_assignment is None and stint.end_reason != "closed"):
        return
    used = tuple(_meaningful(feature, timeline.own_changes(stint)))
    yield stint.end, bool(used), _end_anchor(timeline, stint), used


def _on_transfer(feature, timeline, stint, params) -> Found:
    if stint.end_reason == "transfer":
        used = tuple(_meaningful(feature, timeline.handover_changes(stint)))
        yield stint.end, bool(used), _end_anchor(timeline, stint), used


def _assignee(feature, timeline, stint, params) -> Found:
    if not stint.is_open:
        first = stint.first_assignment
        yield stint.end, first is not None, _end_anchor(timeline, stint), (first,) if first else ()


def _receiver_sets(feature, timeline, stint, params) -> Found:
    following = timeline.next_stint(stint)
    if following is not None:
        hits = tuple(_meaningful(feature, timeline.own_changes(following)))
        yield stint.end, bool(hits), following.changes[0], hits


def _receiver_rewrites(feature, timeline, stint, params) -> Found:
    following = timeline.next_stint(stint)
    if following is None:
        return
    handed_over = timeline.values_at(following.start)
    if not any(handed_over.get(field) for field in feature.fields):
        return
    hits = tuple(c for c in timeline.own_changes(following)
                 if c.field in feature.fields and c.old and c.new and c.old != c.new)
    yield stint.end, bool(hits), following.changes[0], hits


# felismerő -> (leírás: mikor alkalom, és mi számít használatnak / bekövetkezésnek; függvény)
DETECTORS: Dict[str, Tuple[str, Callable[..., Found]]] = {
    "on_hold": ("a csoport hold állapotba tette a ticketet; a mező kitöltése a hold kezdete körül", _on_hold),
    "on_close": ("a csoport zárta le a ticketet; a mező kitöltése a szakaszban", _on_close),
    "on_work": ("a csoportnál érdemi munka folyt (volt felelős, vagy lezárták); a mező kitöltése a szakaszban", _on_work),
    "on_transfer": ("a csoport továbbította a ticketet; a mező kitöltése a továbbítás mentésében", _on_transfer),
    "assignee": ("a szakasz véget ért; volt-e felelőse a csoportnál", _assignee),
    "receiver_sets": ("a csoport továbbította a ticketet; a fogadó kitöltötte a mezőt (pl. hibás továbbítás jelzése)",
                      _receiver_sets),
    "receiver_rewrites": ("a csoport kitöltött mezővel továbbított; a fogadó átírta", _receiver_rewrites),
}


DEFAULT_CATALOG: Tuple[FeatureDef, ...] = tuple(FeatureDef.from_dict(entry) for entry in (
    {"key": "work_notes", "label": "Munkajegyzet", "fields": ["work_notes"], "kind": "on_work", "verb": "ír munkajegyzetet"},
    {"key": "hold_reason", "label": "Hold indoklása", "fields": ["u_pending_reason"], "kind": "on_hold",
     "verb": "ad meg indoklást a holdhoz"},
    {"key": "hold_end_date", "label": "Hold várható vége", "fields": ["u_pending_end_date", "u_pending_end_date_new"],
     "kind": "on_hold", "verb": "ad meg várható véget a holdhoz"},
    {"key": "close_code", "label": "Lezárási kód", "fields": ["close_code"], "kind": "on_close", "verb": "ad meg lezárási kódot"},
    {"key": "close_notes", "label": "Lezárási megjegyzés", "fields": ["close_notes"], "kind": "on_close",
     "verb": "ír lezárási megjegyzést"},
    # az L1 diszpécser-eszköze (a legegyszerűbb esetek továbbításánál); rezolvereknél nem elvárt
    {"key": "routing_instructions", "label": "Továbbítási útmutató", "fields": ["u_routing_instructions"],
     "kind": "on_transfer", "levels": [L1_LEVEL], "verb": "ad továbbítási útmutatót"},
    {"key": "assigned_to", "label": "Felelős kijelölése", "fields": ["assigned_to"], "kind": "assignee",
     "verb": "jelöl ki felelőst"},
    {"key": "wrong_assignment", "label": "Rossz továbbításnak jelölték", "polarity": "-",
     "fields": ["u_wrong_assignment_flag"], "kind": "receiver_sets", "ignore_values": ["0", "false"],
     "verb": "jelölik a továbbításukat rossznak"},
    {"key": "service_rewritten", "label": "Service-t átírták utánuk", "polarity": "-",
     "fields": ["business_service"], "kind": "receiver_rewrites", "verb": "írják át utánuk a service-t"},
))
DEFAULT_CATALOG_PATH = CONFIG_DIR / "feature_catalog.json"


def load_catalog(path: Optional[Path] = DEFAULT_CATALOG_PATH) -> Tuple[FeatureDef, ...]:
    """Az alapkatalógus, a fájl bejegyzéseivel kiegészítve: azonos kulcs felülírja, `"disabled": true`
    kiveszi, új kulcs hozzáadódik. A fájl egy lista (vagy {"features": [...]})."""
    features: Dict[str, FeatureDef] = {feature.key: feature for feature in DEFAULT_CATALOG}
    if path is not None and Path(path).exists():
        data = json.loads(Path(path).read_text(encoding="utf-8") or "[]")
        for entry in data.get("features", []) if isinstance(data, dict) else data:
            if entry.get("disabled"):
                features.pop(entry["key"], None)
            else:
                features[entry["key"]] = FeatureDef.from_dict(entry)
    return tuple(features.values())


def _describe_anchor(change: FieldChange) -> str:
    """Az alkalom eseménye szavakban (mi történt, amikor a funkciót használni kellett volna)."""
    if change.field == "assignment_group":
        return f"továbbította a(z) {change.new} csoportnak" if change.old else f"a(z) {change.new} csoporthoz került"
    if change.field == "state":
        return {"3": "hold állapotba tette", "6": "megoldottnak jelölte", "7": "lezárta"}.get(change.new, f"állapot: {change.new}")
    return f"{change.field}: {change.old or '∅'} → {change.new or '∅'}"


def occasions(timeline: TicketTimeline, params: Mapping[str, Any], catalog: Sequence[FeatureDef] = DEFAULT_CATALOG,
              levels: Mapping[str, str] = {}) -> List[Occasion]:
    found = []
    for stint in timeline.stints:
        for feature in catalog:
            if feature.levels and levels.get(stint.group) not in feature.levels:
                continue
            for at, used, anchor, evidence in DETECTORS[feature.kind][1](feature, timeline, stint, params):
                found.append(Occasion(feature.key, stint.group, timeline.incident_id, at, used, anchor, evidence, stint.index))
    return found


class FeatureUsageDiagnosis(CrossTicketDiagnosis):
    key = "FEATURE_USAGE"
    issue_category = "TOOLING"
    window_type = WindowType.SLIDING
    metric_key = "rate"
    metric_label = "használati arány"
    review_by_default = True   # a "sosem használja" előbb jelölt (tanuló kör), feladat csak elfogadásból
    signal_label = "Funkcióhasználat"

    def __init__(self, catalog_path: Optional[Path] = DEFAULT_CATALOG_PATH):
        self.catalog: Tuple[FeatureDef, ...] = load_catalog(catalog_path)

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("min_occasions", "Legkevesebb alkalom", 5, 1, 1000, 1,
                         "Ennél kevesebb alkalomból nem következtetünk a csoport szokására.", "db"),
            ParameterDef("rare_rate", "Ritka használat", 0.1, 0.0, 1.0, 0.01,
                         "Ennél ritkább (de nem nulla) használat mérsékelt edukációs jelzés (+ funkciónál)."),
            ParameterDef("hold_tolerance_minutes", "Hold-indoklás tűrése", 30, 0, 1440, 5,
                         "A hold kezdetéhez képest ennyi percen belül kitöltött mező számít a hold indoklásának.", "perc"),
            ParameterDef("misuse_min_hits", "Legkevesebb érintett eset", 3, 1, 1000, 1,
                         "− funkciónál legalább ennyi bekövetkezés kell a jelzéshez.", "db"),
            ParameterDef("misuse_rate", "Rossz esetek aránya", 0.2, 0.0, 1.0, 0.01,
                         "− funkciónál ettől az aránytól jelez (mérsékelt)."),
            ParameterDef("misuse_extreme_rate", "Rossz esetek extrém aránya", 0.5, 0.0, 1.0, 0.01,
                         "− funkciónál ettől az aránytól extrém."),
        ]

    @classmethod
    def window_definitions(cls) -> List[ParameterDef]:
        return [ParameterDef(LOOKBACK_KEY, "Visszatekintés", 43200, 1440, 525600, 1440,
                             "Az ennyi percen belüli alkalmakat vizsgálja (43200 perc = 30 nap).", "perc")]

    # --- besorolás és leírás ---

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        n, k = details.get("occasions", 0), details.get("used", 0)
        if n < thresholds["min_occasions"]:
            return None
        if details.get("polarity", 1) < 0:
            if k < thresholds["misuse_min_hits"] or k / n < thresholds["misuse_rate"]:
                return None
            return SeverityLevel.EXCEEDED_EXTREME if k / n >= thresholds["misuse_extreme_rate"] else SeverityLevel.EXCEEDED_MODERATE
        availability = details.get("availability")
        if availability == "unavailable":
            return None
        if k == 0:
            # ismert elérhetőség mellett sosem használja: edukáció; ismeretlennél kérdés az elemzőnek
            return SeverityLevel.EXCEEDED_EXTREME if availability in ("observed", "confirmed") else SeverityLevel.EXCEEDED_MODERATE
        return SeverityLevel.EXCEEDED_MODERATE if k / n <= thresholds["rare_rate"] else None

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        label, group = details.get("label", ""), details.get("group_label", details.get("group", ""))
        n, k = details.get("occasions", 0), details.get("used", 0)
        others = details.get("groups_using", 0)
        if details.get("polarity", 1) < 0:
            return f"{group}: {label} — {k}/{n} alkalommal ({k / n:.0%}), alkalom: {details.get('applies', '')}"
        if details.get("group") == ALL_GROUPS:
            return f"{label}: egyetlen csoport sem használja ({k}/{n} alkalom) — be van vezetve, rajta van az űrlapokon?"
        usage = f"{k}/{n} alkalom" + (f" ({k / n:.0%})" if k else "")
        context = f"más csoportok közül {others} használja" if others else "más csoport sem használja"
        if k == 0 and details.get("availability") == "unknown":
            return f"{group}: sosem használta — {label} ({usage}; {context}). Elérhető nekik?"
        verb = "sosem használja" if k == 0 else "ritkán használja"
        since = f", utoljára {details['last_used_at'][:10]}" if details.get("last_used_at") else ""
        return f"{group}: {verb} — {label} ({usage}{since}; {context}) → edukáció"

    # --- mérés ---

    def signal_items(self, subject: str, details: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
        feature, _, group = subject.partition("|")
        items = (("feature", feature),)
        return items if group == ALL_GROUPS else items + (("assignment_group", group),)

    @staticmethod
    def _availability(known: Mapping[Tuple[str, str], bool], feature: str, group: str, ever_used: bool) -> str:
        """Elérhetőség: tény (a csoportra, vagy "*" mindenkire) > megfigyelt használat > ismeretlen."""
        for key in ((feature, group), (feature, ALL_GROUPS)):
            if key in known:
                return "confirmed" if known[key] else "unavailable"
        return "observed" if ever_used else "unknown"

    def child_units(self, ctx: MeasurementContext) -> Dict[str, Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]]:
        """Almérésekhez jelzésenként (subject): az érintett alkalmak szakaszai (+ funkciónál amikor nem
        használta, − funkciónál amikor bekövetkezett), és az összehasonlítás: ugyanez a funkció ugyanilyen
        helyzetben a többi csoportnál."""
        window = self.resolve_window(ctx)
        params, levels = self.thresholds(ctx), ctx.knowledge.levels
        by_feature: Dict[str, List[Occasion]] = defaultdict(list)
        for incident in ctx.incidents:
            timeline = incident_timeline(incident, ctx.measured_at)
            if timeline is not None:
                for occasion in occasions(timeline, params, self.catalog, levels):
                    if window.contains(occasion.at):
                        by_feature[occasion.feature].append(occasion)
        polarity = {feature.key: feature.polarity for feature in self.catalog}
        result: Dict[str, Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]] = {}
        for feature, found in by_feature.items():
            relevant = [o for o in found if (o.used if polarity.get(feature, 1) < 0 else not o.used)]
            for group in {o.group for o in found}:
                result[f"{feature}|{group}"] = ([(o.incident_id, o.stint) for o in relevant if o.group == group],
                                               [(o.incident_id, o.stint) for o in found if o.group != group])
            result[f"{feature}|{ALL_GROUPS}"] = ([(o.incident_id, o.stint) for o in relevant], [])
        return result

    def observe_incidents(self, incidents, window, ctx):  # a _observe felülírása miatt nem használt
        return ()

    def _observe(self, ctx: MeasurementContext, window: MeasurementWindow) -> Iterable[Observation]:
        params = self.thresholds(ctx)
        known, levels = ctx.knowledge.availability, ctx.knowledge.levels
        everything: List[Occasion] = []
        for incident in ctx.incidents:
            timeline = incident_timeline(incident, ctx.measured_at)
            if timeline is not None:
                everything.extend(occasions(timeline, params, self.catalog, levels))

        last_used: Dict[Tuple[str, str], datetime] = {}
        for occasion in everything:
            if occasion.used:
                key = (occasion.feature, occasion.group)
                last_used[key] = max(last_used.get(key, occasion.at), occasion.at)
        in_window: Dict[Tuple[str, str], List[Occasion]] = defaultdict(list)
        for occasion in everything:
            if window.contains(occasion.at):
                in_window[(occasion.feature, occasion.group)].append(occasion)

        observations = []
        for feature in self.catalog:
            groups = {g: occ for (f, g), occ in in_window.items() if f == feature.key}
            if not groups:
                continue
            users = {g for (f, g) in last_used if f == feature.key}
            totals = (sum(len(occ) for occ in groups.values()), sum(sum(o.used for o in occ) for occ in groups.values()))
            if feature.polarity > 0 and not users and self._availability(known, feature.key, ALL_GROUPS, False) == "unknown":
                # senki nem használja: egyetlen kérdés a funkcióról, nem csoportonként
                observations.append(self._observation(feature, ALL_GROUPS, [o for occ in groups.values() for o in occ],
                                                      "unknown", None, 0, levels, totals))
                continue
            for group, group_occasions in sorted(groups.items()):
                availability = self._availability(known, feature.key, group, (feature.key, group) in last_used)
                observations.append(self._observation(
                    feature, group, group_occasions, availability, last_used.get((feature.key, group)),
                    len(users - {group}), levels, totals,
                ))
        return observations

    def _observation(self, feature: FeatureDef, group: str, group_occasions: List[Occasion], availability: str,
                     last_used_at: Optional[datetime], groups_using: int, levels: Mapping[str, str],
                     totals: Tuple[int, int] = (0, 0)) -> Observation:
        used = [o for o in group_occasions if o.used]
        others_n, others_used = totals[0] - len(group_occasions), totals[1] - len(used)
        # + funkciónál a kihagyott alkalmak, − funkciónál a bekövetkezések a bizonyítékok
        relevant = used if feature.polarity < 0 else [o for o in group_occasions if not o.used]
        refs = []
        for occasion in relevant[:50]:
            refs.append(make_history_ref(occasion.incident_id, occasion.anchor.event, feature.key))
            refs.extend(make_history_ref(occasion.incident_id, c.event, feature.key) for c in occasion.evidence)
        level = levels.get(group)
        details = {
            "feature": feature.key, "label": feature.label, "polarity": feature.polarity, "applies": feature.description,
            "fields": list(feature.fields), "group": group,
            "group_label": f"{group} ({level})" if level else group,
            "occasions": len(group_occasions), "used": len(used),
            "rate": len(used) / len(group_occasions) if group_occasions else 0.0,
            "availability": availability, "groups_using": groups_using,
            "last_used_at": last_used_at.isoformat() if last_used_at else "",
            "verb": feature.verb,
            "others_occasions": others_n, "others_rate": others_used / others_n if others_n > 0 else None,
            # a konkrét alkalmak: mikor kellett volna (+), illetve mikor történt meg (−)
            "examples": [{"incident_id": o.incident_id, "at": o.at.isoformat(), "what": _describe_anchor(o.anchor)}
                         for o in sorted(relevant, key=lambda o: o.at, reverse=True)[:5]],
        }
        return Observation(
            subject=f"{feature.key}|{group}",
            details=details,
            affected=tuple(sorted({o.incident_id for o in relevant})),
            history_refs=tuple(refs),
        )
