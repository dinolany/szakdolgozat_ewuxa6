from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping

from learning.mining.contrast_mining import with_implied
from model.knowledge.predicates import CHILD_DIMENSION_PREFIX


ROLES = {"explain": "magyarázat", "alert": "riasztás"}

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

        Hívja: child_review.review_children."""
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

    Hívja: ChildDef.triggers_label, child_def.trigger_options."""
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
