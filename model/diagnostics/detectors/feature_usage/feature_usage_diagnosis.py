from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from model.diagnostics.core import CrossTicketDiagnosis, LOOKBACK_KEY, MeasurementContext, MeasurementWindow, Observation, ParameterDef, WindowType
from model.diagnostics.detectors.feature_usage.catalog import DEFAULT_CATALOG_PATH, _describe_anchor, load_catalog, occasions
from model.diagnostics.detectors.feature_usage.feature_def import FeatureDef
from model.diagnostics.detectors.feature_usage.occasion import Occasion
from model.diagnostics.history_refs import make_history_ref
from model.knowledge.predicates import ALL_GROUPS
from model.severity_level import SeverityLevel
from model.timeline.timeline_builder import incident_timeline


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
