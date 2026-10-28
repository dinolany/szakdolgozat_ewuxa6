"""Az elemző által findingként elfogadott minták folyamatos mérése.

A minták definíciója (mérés, mezőérték-párok, rossz-küszöb) a diagnosztika saját issue-iban él
(details). Minden futáskor a csúszó ablakban nyitott ticketeken újraméri őket:
- a minta továbbra is kiugró -> ACTIVE (frissül)
- van a mintára illő ticket, de már nem kiugró -> NOT_RELEVANT
- nincs a mintára illő ticket az ablakban -> OUTDATED (a minta elmúlt)
"""

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from learning.mining.contrast_mining import binomial_sf, pattern_key
from learning.mining.measures import MEASURES_BY_KEY
from learning.mining.profile_set import ProfileSet
from learning.mining.profiles import build_profiles, has_value
from learning.mining.ticket_profile import TicketProfile
from model.diagnostics.core import CrossTicketDiagnosis, DiagnosticRun, LOOKBACK_KEY, MeasurementContext, MeasurementWindow, Observation, ParameterDef, WindowType
from model.severity_level import SeverityLevel


Items = Tuple[Tuple[str, str], ...]

def finding_subject(measure: str, items: Iterable[Tuple[str, str]]) -> str:
    return f"{measure}|{pattern_key(items)}"

def _matches(profile: TicketProfile, items: Items) -> bool:
    """Az egység illik-e a mintára (több értékű dimenziónál elég, ha az érték köztük van; a dátumos
    elem tickettnél a nyitás, szakasznál az érkezés napjára vonatkozik)."""
    return all(has_value(profile.dimensions, dim, value) for dim, value in items)

def measure_pattern(
    profiles: Union[ProfileSet, Sequence[TicketProfile]], measure_key: str, items: Items, bad_threshold: float
) -> Optional[Dict[str, Any]]:
    """Egy minta statisztikája a megadott adatlapokon; None, ha nincs a mintára illő egység."""
    measure = MEASURES_BY_KEY[measure_key]
    if isinstance(profiles, ProfileSet):
        profiles = profiles.for_measure(measure)
    population = [p for p in profiles if p.measures.get(measure_key) is not None]
    group = [p for p in population if _matches(p, items)]
    if not population or not group:
        return None
    bad_population = sum(measure.is_bad(p.measures[measure_key], bad_threshold) for p in population)
    bad = [p for p in group if measure.is_bad(p.measures[measure_key], bad_threshold)]
    base_rate = bad_population / len(population)
    rate = len(bad) / len(group)
    return {
        "bad_count": len(bad),
        "group_size": len(group),
        "population": len(population),
        "base_rate": base_rate,
        "rate": rate,
        "lift": rate / base_rate if base_rate else 0.0,
        "p_value": binomial_sf(len(bad), len(group), base_rate) if base_rate else 1.0,
        "bad_profiles": bad,
    }

class PatternFindingDiagnosis(CrossTicketDiagnosis):
    key = "PATTERN_FINDING"
    issue_category = "PATTERN"
    window_type = WindowType.SLIDING
    metric_key = "lift"
    metric_label = "kiemelés (lift)"

    @classmethod
    def threshold_definitions(cls) -> List[ParameterDef]:
        return [
            ParameterDef("min_support", "Legkevesebb érintett ticket", 2, 1, 100, 1, "Ennél kevesebb érintett ticketnél a minta nem aktív.", "db"),
            ParameterDef("min_lift", "Legkisebb kiemelés", 1.5, 1.0, 50, 0.1, "A mintabeli arány legalább ennyiszerese az átlagnak.", "×"),
            ParameterDef("max_p", "Szignifikanciaszint", 0.10, 0.001, 1.0, 0.001, "Egyetlen, előre kiválasztott minta próbája, ezért nincs többszörös tesztelési korrekció."),
            ParameterDef("extreme_lift", "Extrém kiemelés", 5.0, 1.0, 100, 0.5, "Ettől a kiemeléstől extrém a súlyosság (ha elég az érintett ticket is).", "×"),
            ParameterDef("extreme_support", "Extrém legkevesebb érintett ticket", 5, 1, 1000, 1,
                         "Extrém súlyossághoz ennyi érintett ticket is kell (kis mintán a kiemelés félrevezető).", "db"),
        ]

    @classmethod
    def window_definitions(cls) -> List[ParameterDef]:
        return [ParameterDef(LOOKBACK_KEY, "Visszatekintés", 10080, 60, 129600, 60,
                             "A mérés az ennyi percen belül nyitott ticketeken fut (10080 perc = 7 nap).", "perc")]

    def classify(self, details: Mapping[str, Any], thresholds: Mapping[str, Any]) -> Optional[SeverityLevel]:
        if details.get("bad_count", 0) < thresholds["min_support"]:
            return None
        if details.get("lift", 0) < thresholds["min_lift"] or details.get("p_value", 1.0) > thresholds["max_p"]:
            return None
        if details["lift"] >= thresholds["extreme_lift"] and details["bad_count"] >= thresholds["extreme_support"]:
            return SeverityLevel.EXCEEDED_EXTREME
        return SeverityLevel.EXCEEDED_MODERATE

    def describe(self, subject: str, details: Mapping[str, Any]) -> str:
        measure = MEASURES_BY_KEY.get(details.get("measure", ""))
        label = measure.label if measure else details.get("measure", "")
        return (
            f"{label} – {details.get('pattern_key', subject)}: {details.get('bad_count', 0)}/"
            f"{details.get('group_size', 0)} érintett ({details.get('rate', 0):.0%}, átlag "
            f"{details.get('base_rate', 0):.1%}), kiemelés {details.get('lift', 0):.1f}×"
        )

    # --- mérés ---

    def window_profiles(self, ctx: MeasurementContext, window: MeasurementWindow) -> ProfileSet:
        """Az ablakban nyitott ticketek és kezdődött szakaszok adatlapja a mérés pillanatában
        (ugyanaz a populáció, mint a feltárásnál)."""
        profiles = build_profiles(ctx.incidents, ctx.measured_at, window.start or datetime.min, ctx.knowledge.levels,
                                  ctx.knowledge.derived, ctx.knowledge.schedules)
        return ProfileSet(tuple(p for p in profiles.tickets if window.contains(p.opened_at)),
                          tuple(p for p in profiles.stints if window.contains(p.opened_at)))

    def observe_definition(
        self, profiles: ProfileSet, definition: Mapping[str, Any]
    ) -> Optional[Observation]:
        items: Items = tuple(tuple(item) for item in definition["pattern"])
        stats = measure_pattern(profiles, definition["measure"], items, definition["bad_threshold"])
        if stats is None:
            return None
        bad_profiles = stats.pop("bad_profiles")
        details = {**{k: definition[k] for k in ("measure", "pattern", "alternatives", "bad_threshold", "candidate_id")
                      if k in definition}, "pattern_key": pattern_key(items), **stats}
        refs = tuple(ref for profile in bad_profiles for ref in profile.evidence.get(definition["measure"], ()))
        return Observation(
            subject=finding_subject(definition["measure"], items),
            details=details,
            affected=tuple(sorted({p.incident_id for p in bad_profiles})),
            history_refs=refs,
        )

    def observe_incidents(self, incidents, window, ctx):  # a _observe felülírása miatt nem használt
        return ()

    def _observe(self, ctx: MeasurementContext, window: MeasurementWindow) -> Iterable[Observation]:
        definitions = [
            issue.details for issue in ctx.issues
            if issue.diagnostic_key == self.key and not issue.is_deleted and issue.details.get("pattern")
        ]
        if not definitions:
            return []
        profiles = self.window_profiles(ctx, window)
        return [obs for definition in definitions if (obs := self.observe_definition(profiles, definition))]

    def seed_run(self, ctx: MeasurementContext, definition: Mapping[str, Any]) -> DiagnosticRun:
        """Egy újonnan elfogadott minta első mérése (amikor még nincs hozzá issue)."""
        window = self.resolve_window(ctx)
        observation = self.observe_definition(self.window_profiles(ctx, window), definition)
        measurements = [self._to_measurement(observation, ctx, window)] if observation else []
        return DiagnosticRun(self.key, self.window_type, ctx.trigger, ctx.measured_at, window, measurements, frozenset())
