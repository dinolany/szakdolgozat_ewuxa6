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
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from config.paths import CONFIG_DIR
from model.diagnostics.detectors.feature_usage.feature_def import FeatureDef
from model.diagnostics.detectors.feature_usage.occasion import Occasion
from model.diagnostics.detectors.feature_usage.occasion_detectors import DETECTORS
from model.knowledge.predicates import L1_LEVEL
from model.timeline.field_change import FieldChange
from model.timeline.ticket_timeline import TicketTimeline


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
