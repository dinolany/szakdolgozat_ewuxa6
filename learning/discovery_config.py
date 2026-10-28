import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from learning.children.child_review import ChildReviewParams
from learning.feedback.review_params import ReviewParams
from learning.mining.contrast_mining import MiningParams
from learning.mining.measure_def import MeasureDef
from learning.mining.measures import MEASURES
from learning.mining.trend import TREND_DIMENSIONS, TrendParams
from model.diagnostics.core.parameter_def import ParameterDef, resolve_params


MEASURES_SECTION = "measures"

MINING_SECTION = "mining"

QUEUE_SECTION = "queue"

REVIEW_SECTION = "review"

TREND_SECTION = "trend"

DIMENSIONS_SECTION = "dimensions"

# Keresési szerep: 3 kiemelt (saját statisztikai család, előrébb a sorban), 2 teljes (önmagában is minta),
# 1 csak szűkítő, 0 kizárva
DIMENSION_ROLE_DEFAULTS = {"business_service": 3, "assignment_group": 2, "u_responsibility_unit": 2, "location": 2,
                           "subcategory": 1, "impact": 1, "contact_type": 1, "date": 2, "weekday": 2, "hour": 2}

DIMENSION_ROLE_LABELS = {"business_service": "Business service", "assignment_group": "Megoldócsoport",
                         "u_responsibility_unit": "Felelős egység", "location": "Helyszín", "subcategory": "Alkategória",
                         "impact": "Hatás", "contact_type": "Bejelentés módja", "date": "Nap", "weekday": "Hét napja", "hour": "Óra"}

BASELINE_SECTION = "baseline"

_MEASURE_LIMITS = {"duration": (0.5, 720, 0.5), "count": (1, 50, 1), "flag": (1, 1, 1)}

def _measure_definition(measure: MeasureDef) -> ParameterDef:
    """Egy mérés küszöbének paraméterdefiníciója (a Paraméterek fülhöz).

    Hívja: config (modulszint)."""
    minimum, maximum, step = _MEASURE_LIMITS[measure.kind]
    return ParameterDef(
        measure.key, f"{measure.label} küszöb", measure.bad_threshold, minimum, maximum, step,
        f"Ettől az értéktől érintett (kiugró) egy eset. {measure.description}", measure.unit,
    )

DEFINITIONS: Dict[str, List[ParameterDef]] = {
    MEASURES_SECTION: [_measure_definition(measure) for measure in MEASURES],
    MINING_SECTION: [
        ParameterDef("max_order", "Minta legnagyobb mérete", 2, 1, 3, 1, "Legfeljebb ennyi mezőérték alkot egy mintát.", "db"),
        ParameterDef("min_support", "Legkevesebb érintett eset", 3, 1, 100, 1, "Ennél kevesebb érintett esetből nem lesz minta.", "db"),
        ParameterDef("min_lift", "Legkisebb kiemelés", 2.0, 1.0, 50, 0.1, "A mintabeli arány legalább ennyiszerese az átlagnak.", "×"),
        ParameterDef("fdr_q", "Hamis felfedezési arány", 0.10, 0.001, 1.0, 0.001, "Benjamini–Hochberg szint: a talált minták várhatóan legfeljebb ekkora része véletlen."),
        ParameterDef("min_improvement", "Szűkítés legkisebb javulása", 1.2, 1.0, 5, 0.05, "Egy összetett minta ennyivel erősebb kell legyen minden általánosításánál.", "×"),
        ParameterDef("explained_share", "Magyarázott hányad", 0.8, 0.5, 1.0, 0.05, "Egy általános minta kiesik, ha érintett eseteinek ekkora részét egy szűkítése lefedi."),
        ParameterDef("min_distinct_days", "Ismétlődő minta napjai", 1, 1, 30, 1, "Hét napja / óra mintánál legalább ennyi különböző napon kell előfordulnia.", "nap"),
        ParameterDef("top_n", "Legtöbb minta mérésenként", 50, 1, 500, 1, "Mérésenként legfeljebb ennyi minta.", "db"),
        ParameterDef("emphasized_fdr_q", "Kiemelt mezők hamis felfedezési aránya", 0.05, 0.001, 1.0, 0.001,
                     "A kiemelt (3-as szerepű) mezők egyelemes mintái (pl. egy service önmagában) külön családban "
                     "versenyeznek ezzel a szinttel; így néhány tucat próba között nem vesznek el a több ezres keresésben."),
    ],
    QUEUE_SECTION: [
        ParameterDef("lookback_days", "Visszatekintés", 7, 1, 90, 1, "A feltárás az ennyi napon belül nyitott ticketeken fut.", "nap"),
        ParameterDef("daily_limit", "Napi jelöltkeret", 20, 1, 500, 1, "Egyszerre legfeljebb ennyi jelölt kerül az elemző elé.", "db"),
        ParameterDef("decision_capacity", "Napi döntési kapacitás", 10, 1, 500, 1,
                     "Tartós üzemben naponta ennyi elemzői döntés (egy csomópont egy döntés); a jelöltsor eddig mutat, "
                     "és a hangolás ezt a zajhatárt célozza.", "döntés"),
        ParameterDef("training_capacity", "Betanítási kapacitás", 25, 1, 500, 1,
                     "A betanítási fázisban naponta ennyi döntés: kezdetben több címkézett döntés kell a tipphez.", "döntés"),
        ParameterDef("training_decisions", "Betanítás hossza", 150, 0, 100000, 10,
                     "Ennyi tanuló döntésig tart a betanítási fázis (0: nincs betanítás).", "döntés"),
        ParameterDef("extreme_strength", "Áttörő erősség", 6.0, 1.0, 30, 0.5,
                     "Ennél erősebb jelöltet elvető szabály nem rejthet el. Erősség = -log10(q), a 6 q <= 0,000001-et jelent "
                     "(kis mintán a lift félrevezető, ezért a statisztikai erősség dönt)."),
        ParameterDef("watch_days", "Jelzés visszatérése", 7, 1, 90, 1, "A „most nem”-mel félretett jelzés legkorábban ennyi nap múlva jöhet vissza (ha még mindig jelez).", "nap"),
    ],
    DIMENSIONS_SECTION: [
        ParameterDef(dim, f"{DIMENSION_ROLE_LABELS[dim]}: szerep a keresésben", default, 0, 3, 1,
                     "3: kiemelt (az értékei önmagukban külön statisztikai családban versenyeznek – pl. „ez a service "
                     "kiugró” –, saját hamis felfedezési szinttel, és a jelöltjei előrébb kerülnek); 2: teljes (önmagában "
                     "is minta lehet); 1: csak szűkítő (önmagában nem minta, kombinációban csak ha érdemben erősít); "
                     "0: kizárva. Alapmezőknél (amelyeket alig állít bárki) az 1 javasolt.")
        for dim, default in DIMENSION_ROLE_DEFAULTS.items()
    ],
    TREND_SECTION: [
        ParameterDef("enabled", "Bekapcsolva", 1, 0, 1, 1, "1: a saját múlthoz mért romlást is keresi; 0: nem."),
        ParameterDef("history_weeks", "A múlt hossza", 6, 1, 52, 1,
                     "A mostani ablak előtti ennyi hét a csoport saját viszonyítási alapja.", "hét"),
        ParameterDef("min_history", "Legkevesebb múltbeli egység", 20, 1, 10000, 1,
                     "Ennél kevesebb múltbeli ticketnél/szakasznál a csoport saját múltja nem megbízható.", "db"),
        ParameterDef("min_ratio", "Legkisebb romlás", 1.5, 1.0, 50, 0.1,
                     "A mostani arány legalább ennyiszerese a saját múltból várt aránynak.", "×"),
        ParameterDef("prior", "Simítás", 10.0, 0.0, 1000, 1,
                     "Ennyi 'átlagos' egység húzza a szokásos eltérést 1 felé (kis múltnál).", "db"),
    ],
    REVIEW_SECTION: [
        ParameterDef("rule_min_age_days", "Soha nem talált szabály", 14, 1, 365, 1,
                     "Ennyi napos, még soha nem talált szabály felülvizsgálatra kerül.", "nap"),
        ParameterDef("rule_stale_days", "Rég nem talált szabály", 30, 1, 365, 1,
                     "Ennyi napja nem talált szabály felülvizsgálatra kerül.", "nap"),
        ParameterDef("fact_review_days", "Tény megerősítése", 90, 1, 730, 1,
                     "Ennyi napja megerősítetlen tény felülvizsgálatra kerül.", "nap"),
        ParameterDef("lookback_days", "Ellentmondások visszatekintése", 30, 1, 365, 1,
                     "Ennyi napra visszamenőleg keresi az adatban a tényeknek ellentmondó eseményeket.", "nap"),
        ParameterDef("min_contradictions", "Legkevesebb ellentmondás", 3, 1, 100, 1,
                     "Legalább ennyi ellentmondó esemény kell a jelzéshez.", "db"),
        ParameterDef("schedule_coverage", "Munkaidő-javaslat: lefedett aktivitás", 0.75, 0.5, 0.99, 0.01,
                     "A javasolt munkaidő a csoport aktivitásának ekkora részét fedi le (a legszűkebb ilyen sáv). A 0,75 a "
                     "ServiceNow saját munkaidő-mezőjéhez (business_stc) kalibrált: ennél a tanult munkaidő mediánban azt adja ki."),
        ParameterDef("generalize_min_count", "Általánosítási javaslat küszöbe", 3, 2, 100, 1,
                     "Ennyi egyező döntés (pl. ugyanaz a mező zsákutca ennyi bontásnál, ugyanaz az érték ennyi elvetési "
                     "szabályban) után javasolja a gép az általánosítást.", "db"),
        ParameterDef("child_min_runs", "Almérés: futás a „sosem teljesül” jelzésig", 10, 1, 1000, 1,
                     "Ennyi futás után jelez az almérés, ha egyszer sem teljesült.", "db"),
        ParameterDef("child_min_decisions", "Almérés: döntés az egyezés megítéléséhez", 5, 1, 1000, 1,
                     "Ennyi döntés után ítéli meg, egyezik-e a döntéseiddel az almérés jelzése.", "db"),
        ParameterDef("child_min_agreement", "Almérés: legkisebb egyezés", 0.5, 0.0, 1.0, 0.05,
                     "Ennél ritkábban egyező almérés jelez a felülvizsgálaton (félrevisz?)."),
    ],
    BASELINE_SECTION: [
        ParameterDef("min_size", "Legkevesebb ticket", 30, 1, 10000, 1,
                     "A szokásos eltéréshez legalább ennyi ticket kell a minta rögzített (nem átfedő) ablakaiban.", "db"),
        ParameterDef("prior", "Simítás", 10.0, 0.0, 1000, 1,
                     "Ennyi 'átlagos' ticket húzza a szokásos liftet 1,0 felé (kis mintán a lift félrevezető).", "db"),
        ParameterDef("backfill_windows", "Visszamenőleges ablakok", 8, 0, 104, 1,
                     "Ismert eltérésnél ennyi korábbi, nem átfedő ablakot számol újra a rendszer.", "db"),
        ParameterDef("max_p", "Szignifikanciaszint", 0.05, 0.001, 1.0, 0.001,
                     "A saját szokásos eltéréshez képesti romlás próbájának szintje."),
        ParameterDef("min_ratio", "Legkisebb romlás", 1.5, 1.0, 50, 0.1,
                     "A mintabeli arány legalább ennyiszerese a saját szokásos eltérésből várt aránynak.", "×"),
        ParameterDef("min_bad", "Legkevesebb érintett eset", 3, 1, 100, 1,
                     "Ennél kevesebb érintett esetnél az ismert eltérés nem jelez.", "db"),
    ],
}

class DiscoveryConfig:
    """A feltárás állítható paraméterei (mérési küszöbök, keresés, jelöltsor), JSON-ban tárolva."""

    def __init__(self, storage_path: Optional[str] = None):
        """A feltárás paraméterei (alapértékek, a fájlból felülírva).

        Létrehozza: backtest, discovery_cli, tune, synthetic.backtest, synthetic.learning_sim,
        learning.discovery_service."""
        self.storage_path = Path(storage_path) if storage_path else None
        self.values: Dict[str, Dict[str, Any]] = {
            section: resolve_params(definitions, {}) for section, definitions in DEFINITIONS.items()
        }
        self.load()

    def get(self, section: str, key: str) -> Any:
        """Egy paraméter értéke.

        Hívja: DiscoveryCli.cmd_config, DiscoveryCli.cmd_finding, DiscoveryCli.cmd_queue,
        DiscoveryService._brief_queue, DiscoveryService._good_dimension_insights, DiscoveryService._profiles,
        DiscoveryService._refresh_breakdown, DiscoveryService._unit_sets, DiscoveryService.analysis és mások."""
        return self.values[section][key]

    def update(self, section: str, key: str, value: Any) -> Any:
        """Egy paraméter módosítása (határok közé szorítva) és mentése.

        Hívja: DiscoveryCli.cmd_config, DiscoveryService._suppress, DiscoveryService.candidate_dto,
        DiscoveryService.generalize, DiscoveryService.review, DiscoveryService.rule, DiscoveryService.update_config,
        Issue.__init__, Issue.refresh és mások."""
        definition = next((d for d in DEFINITIONS.get(section, []) if d.key == key), None)
        if definition is None:
            raise KeyError(f"Ismeretlen paraméter: {section}.{key}")
        self.values[section][key] = definition.coerce(value)
        self.save()
        return self.values[section][key]

    def dimension_roles(self) -> Dict[str, int]:
        """Mezőnként a keresési szerep (0 kizárva, 1 csak szűkítő, 2 teljes, 3 kiemelt).

        Hívja: DiscoveryConfig.mining_params, DiscoveryConfig.trend_params, DiscoveryService._generalizations,
        learning_sim._day, runner.configure."""
        return {dim: int(value) for dim, value in self.values[DIMENSIONS_SECTION].items()}

    def mining_params(self) -> MiningParams:
        """A mintakeresés paraméterei a szerepekkel együtt.

        Hívja: runner._mine_all."""
        roles = self.dimension_roles()
        return MiningParams(**self.values[MINING_SECTION],
                            excluded=frozenset(d for d, r in roles.items() if r == 0),
                            suppressed=frozenset(d for d, r in roles.items() if r == 1),
                            emphasized=frozenset(d for d, r in roles.items() if r == 3))

    def trend_params(self) -> TrendParams:
        """A romlás-keresés paraméterei.

        Hívja: runner._mine_trends."""
        values = self.values[TREND_SECTION]
        roles = self.dimension_roles()
        return TrendParams(history_weeks=values["history_weeks"], min_history=int(values["min_history"]),
                           min_ratio=values["min_ratio"], prior=values["prior"],
                           fdr_q=self.values[MINING_SECTION]["fdr_q"], min_support=int(self.values[MINING_SECTION]["min_support"]),
                           dimensions=tuple(d for d in TREND_DIMENSIONS if roles.get(d, 2) >= 2))

    def review_params(self) -> ReviewParams:
        """A szabályok és tények felülvizsgálatának paraméterei.

        Hívja: DiscoveryCli.cmd_review, DiscoveryService.review."""
        values = self.values[REVIEW_SECTION]
        return ReviewParams(**{k: v for k, v in values.items() if k in ReviewParams.__dataclass_fields__})

    def schedule_coverage(self) -> float:
        """A munkaidő-javaslat lefedési aránya.

        Hívja: DiscoveryService._schedule_suggestions."""
        return float(self.values[REVIEW_SECTION]["schedule_coverage"])

    def generalize_min_count(self) -> int:
        """Hány egyforma döntéstől jön általánosítási javaslat.

        Hívja: DiscoveryService._generalizations, learning_sim._day."""
        return int(self.values[REVIEW_SECTION]["generalize_min_count"])

    def child_review_params(self) -> ChildReviewParams:
        """Az almérések felülvizsgálata (ugyanabban a szekcióban, child_ előtaggal; a kor a szabályokéval közös).

        Hívja: DiscoveryService.review."""
        values = self.values[REVIEW_SECTION]
        return ChildReviewParams(int(values["child_min_runs"]), int(values["child_min_decisions"]),
                                 float(values["child_min_agreement"]), int(values["rule_min_age_days"]))

    def measure_threshold(self, measure_key: str) -> float:
        """Egy mérés érintett-küszöbe (ettől az értéktől kiugró egy eset).

        Hívja: DiscoveryService._brief_queue, DiscoveryService._good_dimension_insights,
        DiscoveryService._refresh_breakdown, DiscoveryService._unit_sets, DiscoveryService.about,
        DiscoveryService.analysis, runner._counts, runner._mine_all, runner._mine_trends."""
        return self.values[MEASURES_SECTION][measure_key]

    def load(self) -> None:
        """A beállítások betöltése a fájlból.

        Hívja: DiscoveryConfig.__init__."""
        if not self.storage_path or not self.storage_path.exists():
            return
        try:
            stored = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for section, definitions in DEFINITIONS.items():
            self.values[section] = resolve_params(definitions, (stored or {}).get(section))

    def save(self) -> None:
        """A beállítások mentése a fájlba.

        Hívja: DiscoveryConfig.update."""
        if not self.storage_path:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
