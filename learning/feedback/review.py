"""A tudás felülvizsgálata: mely szabályok és tények szorulnak az elemző figyelmére.

A szabálykönyv lassan elavult tudással telik meg, ha senki nem nézi át: egy szabály, amely soha
nem talál, vagy egy tény, amelynek az adat ellentmond, csendben torzítja a feltárást. Jelzések:

Szabály
- soha nem talált (elég régóta él), rég nem talált, lejárt.

Tény
- lejárt, vagy régóta nincs megerősítve;
- ellentmond neki az adat (a megerősítés óta):
  * nem_dolgozik(csoport, nap): a csoport mégis dolgozott azon a napon (saját változásai voltak);
  * nem_elerheto(funkció, csoport): a csoport mégis használja a funkciót;
  * szint(csoport, ...): a csoport nem szerepel az adatban (megszűnt / átnevezték?).

A megerősítés (`reviewed_at`) elnémítja a kor alapú jelzést egy időszakra, és az ellentmondást
csak az azutáni eseményekből számolja: az elemző döntése így nem jön vissza ugyanazért. A tény
felvétele nem megerősítés: ha az adat már akkor ellentmond neki, az azonnal jelez.
"""

from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from learning.feedback.review_item import ReviewItem
from learning.feedback.review_params import ReviewParams
from learning.state.rule import Rule
from model.diagnostics.detectors.feature_usage.catalog import DEFAULT_CATALOG, occasions
from model.diagnostics.detectors.feature_usage.feature_def import FeatureDef
from model.knowledge.fact import Fact
from model.knowledge.predicates import ALL_GROUPS, NEM_DOLGOZIK, NEM_ELERHETO, SZINT
from model.timeline.ticket_timeline import TicketTimeline


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

def _parse(value: str) -> Optional[datetime]:
    """ISO időbélyeg vagy None.

    Hívja: review.review_facts, review.review_rules."""
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None

def _days(since: Optional[datetime], now: datetime) -> float:
    """Két pillanat között eltelt napok.

    Hívja: review.review_facts, review.review_rules."""
    return (now - since).total_seconds() / 86400 if since else 0.0

def review_rules(rules: Iterable[Rule], now: datetime, params: ReviewParams = ReviewParams()) -> List[ReviewItem]:
    """Felülvizsgálandó szabályok: lejárt, soha nem talált, régóta nem talált.

    Hívja: DiscoveryCli.cmd_review, DiscoveryService.review."""
    items = []
    for rule in rules:
        if rule.state != "active":
            continue
        reviewed = _parse(getattr(rule, "reviewed_at", "")) or _parse(rule.created_at)
        until = _parse(rule.valid_until)
        if until and until < now:
            items.append(ReviewItem("rule", rule.rule_id, "expired", f"lejárt ({rule.valid_until[:10]}), még aktívnak jelölt"))
        elif not rule.hits and _days(reviewed, now) >= params.rule_min_age_days:
            items.append(ReviewItem("rule", rule.rule_id, "never_hit",
                                    f"{_days(reviewed, now):.0f} napja él, és még soha nem rejtett el jelöltet"))
        elif rule.hits and _days(max(filter(None, (_parse(rule.last_hit_at), reviewed))), now) >= params.rule_stale_days:
            items.append(ReviewItem("rule", rule.rule_id, "stale",
                                    f"utoljára {rule.last_hit_at[:10]}-én talált ({rule.hits} találat összesen)", rule.hits))
    return items

def review_facts(
    facts: Iterable[Fact],
    timelines: Sequence[TicketTimeline],
    now: datetime,
    params: ReviewParams = ReviewParams(),
    catalog: Sequence[FeatureDef] = DEFAULT_CATALOG,
    levels: Mapping[str, str] = {},
) -> List[ReviewItem]:
    """Felülvizsgálandó tények: az adat ellentmond nekik, vagy nincs nyomuk az adatban.

    Hívja: DiscoveryCli.cmd_review, DiscoveryService.review."""
    facts = [fact for fact in facts if fact.state == "active"]
    lookback_start = now - timedelta(days=params.lookback_days)
    work_days, seen_groups = _group_activity(timelines, lookback_start, now)
    usage = _feature_usage(timelines, catalog, levels, lookback_start, now) \
        if any(fact.predicate == NEM_ELERHETO.name for fact in facts) else Counter()

    items = []
    for fact in facts:
        # az ellentmondás a visszatekintési ablakból számít, a megerősítés előtti események nélkül;
        # a felvétel nem megerősítés: a már az adatban lévő ellentmondás azonnal jelez
        confirmed = _parse(fact.reviewed_at)
        since = max(filter(None, (confirmed, lookback_start)))
        reviewed = confirmed or _parse(fact.created_at)
        until = _parse(fact.valid_until)
        if until and until < now:
            continue  # a lejárt tény már nem hat (is_active); nincs mit felülvizsgálni
        args = fact.args
        if fact.predicate == NEM_DOLGOZIK.name:
            count = sum(1 for moment in work_days.get((args["csoport"], args["nap"]), ()) if moment >= since)
            if count >= params.min_contradictions:
                items.append(ReviewItem("fact", fact.fact_id, "contradicted",
                                        f"a csoport mégis dolgozott {args['nap']} napon: {count} saját változás", count))
                continue
        elif fact.predicate == NEM_ELERHETO.name:
            count = sum(n for (feature, group, moment), n in usage.items()
                        if feature == args["funkcio"] and (args["csoport"] in (group, ALL_GROUPS)) and moment >= since)
            if count >= 1:
                items.append(ReviewItem("fact", fact.fact_id, "contradicted",
                                        f"nem elérhetőnek jelölt funkció, mégis használták: {count} alkalom", count))
                continue
        elif fact.predicate == SZINT.name and timelines and args["csoport"] not in seen_groups:
            items.append(ReviewItem("fact", fact.fact_id, "unseen",
                                    f"a csoport {params.lookback_days} napja nem szerepel az adatban"))
            continue
        if _days(reviewed, now) >= params.fact_review_days:
            items.append(ReviewItem("fact", fact.fact_id, "unconfirmed",
                                    f"{_days(reviewed, now):.0f} napja nincs megerősítve"))
    return items

def _group_activity(timelines: Sequence[TicketTimeline], start: datetime, end: datetime):
    """(csoport, hét napja) -> a csoport saját változásainak időpontjai; és az ablakban látott csoportok.

    Hívja: review.review_facts."""
    work_days: Dict[Tuple[str, str], List[datetime]] = {}
    seen = set()
    for timeline in timelines:
        for stint in timeline.stints:
            if stint.end < start or stint.start > end:
                continue
            seen.add(stint.group)
            for change in timeline.own_changes(stint):
                if start <= change.at <= end and change.field in {"assigned_to", "state", "work_notes", "comments"}:
                    work_days.setdefault((stint.group, _WEEKDAYS[change.at.weekday()]), []).append(change.at)
    return work_days, seen

def _feature_usage(timelines, catalog, levels, start, end) -> Counter:
    """A funkciók használata csoportonként egy időszakban (az elérhetőségi tények ellenőrzéséhez).

    Hívja: review.review_facts."""
    usage: Counter = Counter()
    for timeline in timelines:
        for occasion in occasions(timeline, {}, catalog, levels):
            if occasion.used and start <= occasion.at <= end:
                usage[(occasion.feature, occasion.group, occasion.at)] += 1
    return usage
