"""Konzolos felület a feltárási ciklushoz: feltárás, jelöltsor, döntések, tények és szabályok.

Használat:
    python discovery_cli.py                       # interaktív
    python discovery_cli.py --script demo.txt     # parancsok fájlból (a parancsok kiíródnak)
    python discovery_cli.py --reset               # tiszta állapotból indul
"""

import argparse
import json
import shlex
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from issue_manager import IssueManager
from models.discovery.config import DEFINITIONS, MEASURES_SECTION, DiscoveryConfig
from models.discovery.knowledge import NEM_DOLGOZIK, Candidate, CandidateState, Scope, Verdict
from models.discovery.manager import DiscoveryManager
from models.discovery.measures import MEASURES_BY_KEY
from models.discovery.mining import with_implied
from models.discovery.promotion import promote_finding
from models.discovery.runner import backfill_counts, configure, run_discovery
from models.diagnostics.core import MeasurementContext, Trigger, parse_timestamp
from models.diagnostics.feature_usage import ALL_GROUPS, FeatureUsageDiagnosis
from models.diagnostics.pattern_finding import PatternFindingDiagnosis
from models.history import HistoryLine
from models.incident import Incident
from models.issue import Issue
from models.discovery.review import review_facts, review_rules
from models.knowledge_base import KnowledgeBase
from models.timeline import incident_timeline

BASE_DIR = Path(__file__).resolve().parent
HELP = """
Feltárás
  run <dátum>[T<óra>]        feltárás az adott időpontban (csak dátum: a nap vége), pl. run 2026-08-05
  next [n]                   feltárás n nappal az előző futás után (alapból 1)
  queue                      a döntésre váró jelöltek (sorszámmal)
  show <jelölt>              egy jelölt részletei (sorszám, azonosító vagy kulcsrészlet)
  issues                     a findingokból lett issue-k (minden futás újraméri őket)
Funkcióhasználat (ki milyen ServiceNow-funkciót nem használ)
  usage [funkció]            a jelzések (edukáció, kérdés, rossz esetek); funkciók: usage catalog
  available <funkció> <csoport|*> igen|nem   válasz a kérdésre: elérhető-e a funkció a csoportnak
Döntések
  finding <jelölt>                       finding (issue lesz belőle)
  reject <jelölt> once                   nem finding, csak ez az előfordulás
  reject <jelölt> pattern                nem finding, erre a mintára mindig (kizáró szabály)
  reject <jelölt> rule nem_dolgozik      nem finding, mert a csoport aznap nem dolgozik (tény + szabály)
  reject <jelölt> rule dim=érték[,érték] ...   nem finding, saját szabállyal
  known <jelölt>                         ismert eltérés: ezentúl a saját szokásos eltéréséhez mérve jelez
Tudás
  facts | fact add nem_dolgozik csoport=<sys_id> nap=<Mon..Sun> | fact revoke <FACT-id>
  fact add szint csoport=<sys_id> szint=L1   a csoport L1: továbbítása nem ping-pong, szakaszai nem rezolver mérések
  rules | rule add drop|boost dim=érték[,érték] ... [measure=<kulcs>] | rule revoke <RULE-id> [ok]
  hidden [RULE-id]           mit takarnak a szabályok
  review                     felülvizsgálandó szabályok és tények (soha nem talált, elavult, ellentmond az adat)
  fact confirm <FACT-id> | rule confirm <RULE-id>   megerősítés: továbbra is igaz
Egyéb
  log | stats | config [szekció kulcs érték] | reset | help | quit
"""


def short(value: str, width: int = 10) -> str:
    return value if len(value) <= width + 2 else value[:width] + "…"


def short_key(key: str) -> str:
    return " & ".join(f"{part.split('=', 1)[0]}={short(part.split('=', 1)[1])}" for part in key.split("&"))


def measure_label(key: str) -> str:
    measure = MEASURES_BY_KEY.get(key)
    return measure.label if measure else key


class DiscoveryCli:
    def __init__(self, state_path: Path, config_path: Path, timeline_path: Path, knowledge_path: Path):
        self.state_path = state_path
        self.config = DiscoveryConfig(str(config_path))
        self.timeline_path = timeline_path
        self.knowledge = KnowledgeBase(path=str(knowledge_path))
        state = self._load_state()
        # Régi állapot: a tények még a feltárási állapotban voltak; átkerülnek a közös tudástárba
        moved = self.knowledge.import_facts(state.pop("facts", {}) or {})
        if moved:
            print(f"[tudástár] {moved} tény átvéve a feltárási állapotból: {knowledge_path.name}")
        self.manager = DiscoveryManager(state, knowledge=self.knowledge)
        self.incidents: Optional[List[Incident]] = None
        self.listing: List[str] = []
        self.issue_manager = IssueManager()
        self.finding_diagnostic = PatternFindingDiagnosis()
        self.usage_diagnostic = FeatureUsageDiagnosis()

    @property
    def findings_db(self) -> Dict:
        """A findingokból lett issue-k tárolója (a konzolon a feltárási állapot része)."""
        return self.manager.state.setdefault("findings", {"issues": {}, "issue_history": []})

    def _finding_context(self, trigger: str) -> MeasurementContext:
        issues = [i if isinstance(i, Issue) else Issue.from_dict(i) for i in self.findings_db["issues"].values()]
        return MeasurementContext(trigger, datetime.fromisoformat(self.manager.now), self._load_incidents(), issues,
                                  knowledge=self.manager.knowledge_view())

    # --- tárolás és adatok ---

    def _load_state(self) -> Dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        return {}

    def save(self) -> None:
        issues = self.findings_db["issues"]
        for issue_id, issue in list(issues.items()):
            if isinstance(issue, Issue):
                issues[issue_id] = issue.to_dict()
        self.state_path.write_text(json.dumps(self.manager.state, ensure_ascii=False, indent=1, default=str),
                                   encoding="utf-8")

    def _load_incidents(self) -> List[Incident]:
        if self.incidents is None:
            rows = json.loads(self.timeline_path.read_text(encoding="utf-8"))["rows"]
            incidents: Dict[str, Incident] = {}
            for row in sorted(rows, key=lambda r: r["_virtual_dt"]):
                incident_id = row.get("set") or row.get("incident")
                incidents.setdefault(incident_id, Incident(incident_id)).apply_event(HistoryLine(row))
            self.incidents = list(incidents.values())
            print(f"[adat] {len(self.incidents)} incidens betöltve: {self.timeline_path.name}")
        return self.incidents

    # --- segédek ---

    def select(self, selector: str) -> Candidate:
        if selector.isdigit() and 1 <= int(selector) <= len(self.listing):
            return self.manager.candidate(self.listing[int(selector) - 1])
        if selector in self.manager.state["candidates"]:
            return self.manager.candidate(selector)
        ordered = [self.manager.candidate(cid) for cid in self.listing] + self.manager.candidates()
        for candidate in ordered:  # pontos kulcsegyezés előnyben
            if selector == candidate.key or selector in candidate.alternatives:
                return candidate
        for candidate in ordered:
            if selector in candidate.key or any(selector in alt for alt in candidate.alternatives):
                return candidate
        raise ValueError(f"Nincs ilyen jelölt: {selector}")

    @staticmethod
    def parse_conditions(tokens: List[str]):
        conditions, measures = [], []
        for token in tokens:
            dim, _, values = token.partition("=")
            if not values:
                raise ValueError(f"Hibás feltétel: {token} (dim=érték[,érték] kell)")
            if dim == "measure":
                measures.extend(values.split(","))
            else:
                conditions.append({"dim": dim, "values": values.split(",")})
        return conditions, measures or None

    def describe(self, candidate: Candidate) -> str:
        s = candidate.stats
        flags = f" [{', '.join(candidate.flags)}]" if candidate.flags else ""
        if candidate.covered_by:
            flags += f" [lefedi: {short_key(candidate.covered_by)}]"
        alternatives = f" (+{len(candidate.alternatives)} azonos halmazú leírás)" if candidate.alternatives else ""
        return (f"{measure_label(candidate.measure):15} {short_key(candidate.key)}{alternatives}{flags}\n"
                f"      {s.get('bad_count')}/{s.get('group_size')} érintett ({s.get('rate', 0):.0%}, átlag "
                f"{s.get('base_rate', 0):.1%}), lift {s.get('lift', 0):.1f}, q={s.get('q_value', 1):.1e}, "
                f"napok={s.get('distinct_days')}, prioritás {candidate.priority:.1f}"
                + self.describe_baseline(candidate))

    @staticmethod
    def describe_baseline(candidate: Candidate) -> str:
        if not candidate.baseline:
            return ""
        b, cmp = candidate.baseline, candidate.stats.get("baseline_comparison")
        text = (f"\n      ismert eltérés: szokásos lift {b['lift']:.1f} (nyers {b['raw_lift']:.1f}, "
                f"{b['bad']}/{b['size']} érintett, {b['windows']} ablak, befagyasztva {b['frozen_at'][:10]})")
        if cmp:
            text += (f"\n      most: {cmp['rate']:.0%} vs. elvárt {cmp['expected_rate']:.1%} "
                     f"(átlag {cmp['base_rate']:.1%} × {b['lift']:.1f}), romlás {cmp['ratio']:.1f}×, p={cmp['p_value']:.1e}")
        return text

    # --- parancsok ---

    def cmd_run(self, args):
        as_of = parse_timestamp(args[0] if "T" in args[0] else args[0] + "T23:59:59")
        if as_of is None:
            raise ValueError("Hibás dátum.")
        report = run_discovery(self.manager, self._load_incidents(), as_of, self.config)
        print(f"[feltárás] {as_of}: {len(report.new)} új, {len(report.updated)} ismert jelölt, "
              f"{len(report.filtered)} szabály által rejtve, {len(report.returned)} visszatért, "
              ""
              f"{len(report.exceeds_baseline)} ismert eltérés romlott")
        if self.findings_db["issues"]:
            run = self.finding_diagnostic.run(self._finding_context(Trigger.EVENT_BATCH))
            changes = self.issue_manager.apply_run(self.findings_db, run, "diagnostic_rerun")
            print(f"[findingok] {len(run.measurements)} finding újramérve, {len(changes)} állapotváltozás"
                  + "".join(f"\n   {c['issue_id']}: {c['old_state']} → {c['new_state']}" for c in changes))
        self._run_usage(Trigger.EVENT_BATCH, quiet=True)
        self.cmd_queue([])

    def _run_usage(self, trigger: str, quiet: bool = False):
        run = self.usage_diagnostic.run(self._finding_context(trigger))
        changes = self.issue_manager.apply_run(self.findings_db, run, "diagnostic_rerun")
        active = [m for m in run.measurements if m.exceeded]
        if quiet:
            print(f"[funkcióhasználat] {len(active)} jelzés, {len(changes)} állapotváltozás ('usage' a részletekhez)")
        return active

    def cmd_usage(self, args):
        if args[:1] == ["catalog"]:
            for feature in self.usage_diagnostic.catalog:
                sign = "+" if feature.polarity > 0 else "−"
                scope = f" [csak: {', '.join(sorted(feature.levels))}]" if feature.levels else ""
                print(f" {sign} {feature.key:22} {feature.label:30} mező: {', '.join(feature.fields)}{scope}"
                      f"\n     {feature.kind}: {feature.description}")
            return
        active = [m for m in self._run_usage(Trigger.MANUAL) if not args or m.details["feature"] == args[0]]
        if not active:
            print("Nincs funkcióhasználati jelzés.")
            return
        kinds = (("Edukáció (elérhető, de nem használják)", lambda d: d["polarity"] > 0 and d["availability"] in ("observed", "confirmed")),
                 ("Kérdés: elérhető-e? (válasz: available <funkció> <csoport> igen|nem)", lambda d: d["polarity"] > 0 and d["availability"] == "unknown"),
                 ("Rossz esetek (− funkciók)", lambda d: d["polarity"] < 0))
        for title, belongs in kinds:
            items = sorted((m for m in active if belongs(m.details)), key=lambda m: (str(m.severity), -m.details["occasions"]))
            if items:
                print(f"{title} ({len(items)}):")
                for m in items:
                    print(f"  [{m.severity}] {m.description}")

    def cmd_available(self, args):
        if len(args) != 3 or args[2] not in ("igen", "nem"):
            raise ValueError("available <funkció> <csoport|*> igen|nem")
        feature, group = args[0], args[1]
        if feature not in {f.key for f in self.usage_diagnostic.catalog}:
            raise ValueError(f"Ismeretlen funkció: {feature} (usage catalog)")
        if group != ALL_GROUPS:
            group = self._resolve_group(group)
        predicate = "elerheto" if args[2] == "igen" else "nem_elerheto"
        fact = self.manager.add_fact(predicate, {"funkcio": feature, "csoport": group})
        print(f"Tény: {fact.fact_id} {fact.render()}")

    def _resolve_group(self, prefix: str) -> str:
        """Csoport azonosító előtagból (a funkcióhasználati issue-k csoportjai közül)."""
        groups = {issue["details"].get("group") if isinstance(issue, dict) else issue.details.get("group")
                  for issue in self.findings_db["issues"].values()}
        matches = sorted(g for g in groups if g and g.startswith(prefix))
        if len(matches) > 1:
            raise ValueError(f"Több csoport illik: {', '.join(short(m) for m in matches[:5])}")
        return matches[0] if matches else prefix

    def cmd_next(self, args):
        last = self.manager.state["meta"].get("last_run", {}).get("measured_at")
        if not last:
            raise ValueError("Még nem volt futás: használd a 'run <dátum>' parancsot.")
        days = int(args[0]) if args else 1
        self.cmd_run([(datetime.fromisoformat(last) + timedelta(days=days)).isoformat()])

    def cmd_queue(self, args):
        queue = self.manager.queue(self.config.get("queue", "daily_limit"))
        self.listing = [c.candidate_id for c in queue]
        if not queue:
            print("A jelöltsor üres.")
            return
        print(f"Jelöltsor ({len(queue)}):")
        for index, candidate in enumerate(queue, 1):
            tip = candidate.suggestion
            tip_text = f"\n      tipp: {tip['verdict']} ({tip['confidence']:.2f})" if tip else ""
            print(f" {index:2}. {self.describe(candidate)}{tip_text}")

    def cmd_show(self, args):
        c = self.select(args[0])
        print(f"{c.candidate_id}  állapot: {c.state}\n {self.describe(c)}")
        print(f"  ablak: {c.window_start} → {c.window_end}; először: {c.first_seen_at}; utoljára: {c.last_seen_at}")
        for alternative in c.alternatives:
            print(f"  azonos halmaz: {short_key(alternative)}")
        print(f"  érintett ticketek: {', '.join(c.bad_incident_ids)}")
        if c.suggestion:
            print(f"  tipp: {c.suggestion['verdict']} ({c.suggestion['confidence']:.2f})")
            for reason in c.suggestion["reasons"]:
                print(f"     hasonló: {reason}")
        if c.filtered_by:
            print(f"  elrejtette: {c.filtered_by}")
        print(f"  előfordulások: {len(c.occurrences)} ({', '.join(o['measured_at'][:10] for o in c.occurrences[-5:])})")
        if c.counts:
            print(f"  darabszámok ({len(c.counts)} ablak): " + ", ".join(
                f"{r['window_end'][:10]} {r['bad']}/{r['size']}" for r in c.counts[-5:]))

    def cmd_finding(self, args):
        c = self.select(args[0])
        self.manager.decide(c.candidate_id, Verdict.FINDING.value, note=" ".join(args[1:]))
        issue = promote_finding(
            self.findings_db, self.issue_manager, self.finding_diagnostic, c,
            self._finding_context(Trigger.MANUAL), self.config.get(MEASURES_SECTION, c.measure),
        )
        self.manager.link_issue(c.candidate_id, issue.issue_id if issue else None)
        if issue is None:
            print(f"Finding: {short_key(c.key)} — az ablakban nincs a mintára illő ticket, issue nem jött létre")
        else:
            print(f"Finding: {short_key(c.key)} ({measure_label(c.measure)}) → {issue.issue_id} [{issue.state.value}]"
                  f"\n   {issue.description}")

    def cmd_issues(self, args):
        issues = [i if isinstance(i, Issue) else Issue.from_dict(i) for i in self.findings_db["issues"].values()]
        if not issues:
            print("Nincs findingból lett issue.")
            return
        for issue in sorted(issues, key=lambda i: i.issue_id):
            print(f" {issue.issue_id} [{issue.state.value}] {issue.severity}  ablak: {issue.window_start[:10]} → "
                  f"{issue.window_end[:10]}, mérve: {issue.measured_at[:16]}, aktiválta: {issue.trigger}"
                  f"\n     {issue.description}")

    def cmd_reject(self, args):
        if len(args) < 2:
            raise ValueError("reject <jelölt> once|pattern|rule ...")
        c = self.select(args[0])
        mode = args[1]
        if mode == "once":
            self.manager.decide(c.candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_OCCURRENCE.value)
            print(f"Elvetve erre az előfordulásra: {short_key(c.key)}")
        elif mode == "pattern":
            decision = self.manager.decide(c.candidate_id, Verdict.NOT_FINDING.value, Scope.THIS_PATTERN.value)
            print(f"Elvetve mindig: {short_key(c.key)} → {decision.rule_id}")
        elif mode == "rule" and len(args) >= 3 and args[2] == NEM_DOLGOZIK.name:
            values = dict(with_implied(c.items))
            if "assignment_group" not in values or "weekday" not in values:
                raise ValueError("A jelöltben nincs csoport és nap: a nem_dolgozik tény nem képezhető belőle.")
            decision = self.manager.decide(
                c.candidate_id, Verdict.NOT_FINDING.value, Scope.RULE.value,
                fact=(NEM_DOLGOZIK.name, {"csoport": values["assignment_group"], "nap": values["weekday"]}),
            )
            print(f"Tény: {self.manager.state['facts'][decision.fact_id]['predicate']}"
                  f"({short(values['assignment_group'])}, {values['weekday']}) → {decision.rule_id}")
            self._report_hidden()
        elif mode == "rule":
            conditions, measures = self.parse_conditions(args[2:])
            decision = self.manager.decide(c.candidate_id, Verdict.NOT_FINDING.value, Scope.RULE.value,
                                           rule_conditions=conditions, rule_measures=measures)
            print(f"Saját szabály: {decision.rule_id}")
            self._report_hidden()
        else:
            raise ValueError("A mód: once | pattern | rule nem_dolgozik | rule dim=érték ...")

    def cmd_known(self, args):
        c = self.select(args[0])
        configure(self.manager, self.config)
        filled = backfill_counts(self.manager, self._load_incidents(), c, datetime.fromisoformat(self.manager.now),
                                 self.config)
        self.manager.decide(c.candidate_id, Verdict.KNOWN_DEVIATION.value, note=" ".join(args[1:]))
        print(f"Ismert eltérés: {short_key(c.key)} ({filled} korábbi ablak visszamenőleg)"
              f"{self.describe_baseline(self.manager.candidate(c.candidate_id))}")

    def _report_hidden(self):
        hidden = [c for c in self.manager.candidates() if c.state == CandidateState.FILTERED.value]
        print(f"  jelenleg {len(hidden)} jelöltet rejtenek a szabályok")

    def cmd_facts(self, args):
        facts = self.manager.facts()
        for fact in facts:
            print(f" {fact.fact_id} [{fact.state}] {fact.render()}  {fact.note}")
        if not facts:
            print("Nincs tény.")

    def cmd_fact(self, args):
        if args[:1] == ["add"]:
            values = dict(token.split("=", 1) for token in args[2:])
            fact = self.manager.add_fact(args[1], values)
            print(f"Tény: {fact.fact_id} {fact.render()}")
            self._report_hidden()
        elif args[:1] == ["revoke"]:
            fact = self.manager.revoke_fact(args[1])
            print(f"Visszavonva: {fact.fact_id}")
        elif args[:1] == ["confirm"]:
            fact = self.knowledge.confirm_fact(args[1], self.manager.now)
            print(f"Megerősítve: {fact.fact_id} {fact.render()}")
        else:
            raise ValueError("fact add ... | fact revoke <id> | fact confirm <id>")

    def cmd_rules(self, args):
        rules = self.manager.rules()
        for rule in rules:
            print(f" {rule.rule_id} v{rule.version} [{rule.state}] {rule.kind}/{rule.action} "
                  f"találat: {rule.hits} ({rule.source})\n     {rule.render()}")
        if not rules:
            print("Nincs szabály.")

    def cmd_rule(self, args):
        if args[:1] == ["add"]:
            conditions, measures = self.parse_conditions(args[2:])
            rule = self.manager.add_rule(conditions, action=args[1], measures=measures)
            print(f"Szabály: {rule.rule_id} {rule.render()}")
            self._report_hidden()
        elif args[:1] == ["revoke"]:
            rule = self.manager.revoke_rule(args[1], " ".join(args[2:]))
            print(f"Visszavonva: {rule.rule_id} (v{rule.version})")
            self._report_hidden()
        elif args[:1] == ["confirm"]:
            rule = self.manager.confirm_rule(args[1])
            print(f"Megerősítve: {rule.rule_id}")
        else:
            raise ValueError("rule add drop|boost ... | rule revoke <id> | rule confirm <id>")

    def cmd_review(self, args):
        if not self.manager.now:
            raise ValueError("Még nem volt futás: a felülvizsgálat a mérés (virtuális) idejéhez igazodik.")
        now = datetime.fromisoformat(self.manager.now)
        params = self.config.review_params()
        view = self.manager.knowledge_view()
        timelines = [t for i in self._load_incidents() if (t := incident_timeline(i, now)) is not None]
        items = review_rules(self.manager.rules(), now, params) + review_facts(
            self.knowledge.facts(), timelines, now, params, self.usage_diagnostic.catalog, view.levels)
        if not items:
            print("Nincs felülvizsgálandó szabály vagy tény.")
            return
        rules = {r.rule_id: r for r in self.manager.rules()}
        facts = {f.fact_id: f for f in self.knowledge.facts()}
        for item in items:
            shown = rules[item.object_id].render() if item.kind == "rule" else facts[item.object_id].render()
            print(f" {item.object_id} [{item.reason}] {shown}\n     {item.message}")
        print("Teendő: <rule|fact> confirm <id> (továbbra is igaz) vagy <rule|fact> revoke <id>")

    def cmd_hidden(self, args):
        hidden = self.manager.covered_by(args[0]) if args else [
            c for c in self.manager.candidates() if c.state == CandidateState.FILTERED.value]
        for candidate in hidden:
            by = candidate.filtered_by or {}
            print(f" {measure_label(candidate.measure):15} {short_key(candidate.key)}  ← {by.get('rule_id')}"
                  f"{' / ' + by['fact_id'] if by.get('fact_id') else ''}")
        if not hidden:
            print("Nincs rejtett jelölt.")

    def cmd_log(self, args):
        for decision in self.manager.decisions():
            candidate = self.manager.candidate(decision.candidate_id)
            tip = decision.suggestion_snapshot
            tip_text = f", tipp: {tip['verdict']}" if tip else ""
            print(f" {decision.decision_id} {decision.at[:16]} {decision.verdict}"
                  f"{'/' + decision.scope if decision.scope else ''}{tip_text}  {short_key(candidate.key)}")

    def cmd_stats(self, args):
        stats = self.manager.learning_stats()
        states: Dict[str, int] = {}
        for candidate in self.manager.candidates():
            states[candidate.state] = states.get(candidate.state, 0) + 1
        print(f" döntések: {stats['decisions']} (tanuló: {stats['learning_decisions']}), "
              f"tippel együtt: {stats['with_suggestion']}, egyetértés: {stats['agreement']}")
        print(f" jelöltek állapot szerint: {states}")

    def cmd_config(self, args):
        if len(args) == 3:
            value = self.config.update(args[0], args[1], args[2])
            print(f"{args[0]}.{args[1]} = {value}")
            return
        for section, definitions in DEFINITIONS.items():
            print(f"[{section}]")
            for definition in definitions:
                print(f"  {definition.key:20} = {self.config.get(section, definition.key):<8} {definition.label}")

    def cmd_reset(self, args):
        self.manager = DiscoveryManager({}, knowledge=self.knowledge)
        self.listing = []
        print("Feltárási állapot törölve (a tudástár tényei megmaradnak).")

    # --- futtatás ---

    def execute(self, line: str) -> bool:
        tokens = shlex.split(line, comments=True)
        if not tokens:
            return True
        command, args = tokens[0].lower(), tokens[1:]
        if command in {"quit", "exit"}:
            return False
        if command == "help":
            print(HELP)
            return True
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            print(f"Ismeretlen parancs: {command} (help)")
            return True
        try:
            handler(args)
            self.save()
        except (ValueError, KeyError, IndexError) as error:
            print(f"Hiba: {error}")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Feltárási ciklus konzolon")
    parser.add_argument("--script", help="parancsfájl")
    parser.add_argument("--state", default=str(BASE_DIR / "discovery_state.json"))
    parser.add_argument("--config", default=str(BASE_DIR / "config" / "discovery.json"))
    parser.add_argument("--timeline", default=str(BASE_DIR / "transformed_timeline.json"))
    parser.add_argument("--knowledge", default=str(BASE_DIR / "knowledge.json"), help="a közös tudástár (tények)")
    parser.add_argument("--reset", action="store_true", help="tiszta feltárási állapotból indul (a tudástár marad)")
    args = parser.parse_args()

    state_path = Path(args.state)
    if args.reset and state_path.exists():
        state_path.unlink()
    cli = DiscoveryCli(state_path, Path(args.config), Path(args.timeline), Path(args.knowledge))

    if args.script:
        for line in Path(args.script).read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#") or not line.strip():
                if line.strip():
                    print(f"\n{line.strip()}")
                continue
            print(f"\n> {line.strip()}")
            if not cli.execute(line):
                break
        return

    print("Feltárási ciklus — 'help' a parancsokhoz.")
    while True:
        try:
            line = input("\nfeltárás> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cli.execute(line):
            break


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
