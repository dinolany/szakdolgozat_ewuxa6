# Diagnosztikai szerkezet

Ez a leírás a diagnosztikák egyetlen keretrendszerét mutatja be (`models/diagnostics/core`).
Régi, párhuzamos osztályszerkezet nincs: minden diagnosztika ezen fut.

## Rétegek

```
history sorok (HistoryLine)
   │
   ▼
models/timeline.py ─────────── a ticket útja egy adott pillanatig (as_of):
   │                            változások, csoportszakaszok (Stint), hold, saját / átadási változások
   │
   ├──► models/discovery/ ───── feltárás: mintakeresés → jelölt → elemzői döntés → finding / szabály
   │                            (a "tanuló" kör; jelöltsor, tipp, ismert eltérés, felülvizsgálat)
   │
   └──► models/diagnostics/ ─── diagnosztikák: mérés → besorolás → Measurement → Issue
              │
              ▼
        issue_manager.py ────── az issue-k életciklusa (ACTIVE / NOT_RELEVANT / OUTDATED ...)

models/knowledge_base.py ────── közös tudástár (tények): a feltárás írja, minden diagnosztika olvassa
```

Két alapelv:

1. **A diagnosztika a history-ból dolgozik, a mérés pillanatáig nézve** (`incident_timeline(incident, ctx.measured_at)`),
   soha nem az incidens éppen beírt mezőiből. A history-ban minden benne van; bármely korábbi
   pillanat állapota visszaállítható, és a jövőbeli események nem szivárognak be.
2. **A csoportra vonatkozó mérés szakaszonként értendő.** Egy ticket sokszor jár csoportok között;
   a csoportot csak az a szakasz terheli, amikor nála volt (`Stint`), és a csoport saját változásai
   (`TicketTimeline.own_changes`) – a küldő továbbítási mentése a küldőé.

## A mérés útja

```
DiagnosticRegistry.execute(trigger, measured_at, incidents, küszöbök, ablakok, knowledge)
  └─ diagnosztikánként (fázis szerint: 1 = ticketek, 2 = issue-k):
       ctx = MeasurementContext(trigger, measured_at, incidents, issues, thresholds, window_params,
                                batch_events, knowledge)
       run = diagnostic.run(ctx)
           ├─ window = resolve_window(ctx)                 (NONE / SNAPSHOT / SLIDING / TUMBLING)
           ├─ _observe(ctx, window) → Observation-ök        (a család végzi; a konkrét diagnosztika mér)
           ├─ classify(details, thresholds) → súlyosság     (a konkrét diagnosztika; None = nincs túllépés)
           ├─ describe(subject, details) → leírás
           └─ Measurement-ek (túllépés, vagy már létező issue újramérése)
       IssueManager.apply_run(db, run) → issue-állapotváltozások
```

| Objektum | Mi ez |
|---|---|
| `MeasurementContext` | a futás bemenete: idő, incidensek, issue-k, a diagnosztika saját küszöbei és ablakparaméterei, a tudástár nézete (`knowledge`) |
| `Observation` | amit a diagnosztika egy *subject*-ről megállapít, küszöbtől függetlenül: `details`, érintett objektumok, history-hivatkozások |
| `Measurement` | besorolt eredmény (súlyosság, metrika, leírás); ebből lesz vagy frissül az issue |
| `DiagnosticRun` | egy diagnosztika egy futásának összes mérése + az ablak |

## Családok (alaposztályok)

| Család | Mit vizsgál | A diagnosztika feladata | Ablak |
|---|---|---|---|
| `SingleTicketDiagnosis` | egy ticket életútját | `accumulate(incident, ctx)`: időrendben lépésenként a details (a küszöbátlépés eseménye `trigger_ref` lesz) | NONE |
| `CrossTicketDiagnosis` | több ticket közös mintázatát | `_observe(ctx, window)` (vagy `observe_incidents`): subjectenként egy Observation | SLIDING / TUMBLING / SNAPSHOT |
| `CrossIssueDiagnosis` | más diagnosztikák issue-it (2. fázis) | `observe_issues(issues, window, ctx)` | SNAPSHOT |

Minden diagnosztika megadja még: `key`, `config_key` (a paraméterek kulcsa), `issue_category`,
`metric_key` / `metric_label`, `threshold_definitions()`, időablaknál `window_definitions()`
(SLIDING: `lookback_minutes`, TUMBLING: `bucket_minutes`), `classify`, `describe`.
Régi formátumú tárolt issue-k megtartásához felülírható az `issue_id_for(occurrence)`.

## Ablakok és az issue-k sorsa

| Ablak | Jelentés | Ha a subject kiesik a mérésből |
|---|---|---|
| `NONE` | a ticket teljes élete | NOT_RELEVANT |
| `SNAPSHOT` | állapotkép a mérés pillanatában | NOT_RELEVANT |
| `SLIDING` | `[vég − lookback, vég]`, mérésről mérésre csúszik | OUTDATED (az ablak elmúlt) |
| `TUMBLING` | igazított, egymást nem fedő vödrök; minden vödör külön előfordulás | korábbi vödör issue-ja: OUTDATED |

Issue-állapotok (`models/issue.py`): ACTIVE (túllépés), NOT_RELEVANT (mérve, küszöb alatt),
OUTDATED (időablakos, elmúlt), valamint a felhasználói állapotok (RESOLVED, SUPPRESSED, DECLINED,
ARCHIVED, DELETED), amelyekhez a mérés nem nyúl.

## Issue = feladat: jóváhagyásos mód

Az issue feladat, nem automatikus megfigyelés. Diagnosztikánként beállítható (Thresholdok nézet,
„Feladat csak jóváhagyással”; tárolás: `database.json.thresholds.json` → `review`):

- **automatikus**: a túllépésből azonnal issue lesz (pl. csoport nélküli ticket – sürgős, egyedi teendő);
- **jóváhagyásos**: a diagnosztika nem nyit új issue-t (`MeasurementContext.review`); a jelzései
  (`BaseDiagnosis.signals`) a feltárással együtt, naponta a Tanulás jelöltsorába kerülnek (`kind="signal"`),
  ugyanúgy, mint a mintajelöltek: tipp, csomópont (`signal_items`), döntési keret, szabály. Elfogadásból lesz
  feladat (`accept_signal`), amelyet a diagnosztika ezután minden kötegnél újramér és lezár, ha megszűnt.

A `FEATURE_USAGE` alapból jóváhagyásos (`review_by_default`); az elérhetőségi kérdések (ismeretlen
elérhetőség) külön, a Javaslatok › Kérdések alatt jelennek meg, és nem fogyasztják a döntési keretet.

A jelzéskártya szavakban mondja el, mi a gond: a fejléc a katalógus `verb` mezőjéből készül (pl. „X: sosem ír
munkajegyzetet”), alatta áll, mikor várható a használat, hány alkalomból hányszor történt meg, és mekkora arányban
használják a többi csoportok ugyanilyen helyzetben (`others_rate`). A legutóbbi legfeljebb 5 alkalom (`examples`)
megmutatja, mi történt a funkció helyett (pl. „továbbította a(z) Y csoportnak – munkajegyzet nélkül”). Ha ugyanazt a
funkciót több csoport sem használja, a jelzések egy csomópontba kerülnek (`feature` dimenzió).

## Lezárás tanulsággal

Bármely issue (bármely diagnosztikából) lezárható tanulsággal (`models/discovery/lessons.py`, issue-panel ›
„Lezárás tanulsággal”, `POST /api/issues/lesson`). Az issue a nyomozás helye; a lezáráskor megadott tanulság a
tudásba kerül, így a következő hasonló esetnél a gép már tudja:

- valódi hiba: a findinghoz vezető döntés megerősítést kap (`scope="confirmed"`, erősebb súly a tippben);
- munkaidőn kívüli várakozás: `nem_dolgozik(csoport, nap)` tények (tudásszabály: az időfüggő jelöltek elvetése);
- a funkció ennél a csoportnál nem elvárt: `nem_elvart(funkció, csoport)` tény (tudásszabály: a jelzés nem jön);
- nem hiba: szabály – feltárási mintánál a minta, diagnosztikánál az elemző által kiválasztott leíróelemek
  (egyedi jegyre szóló szabály nem képződik), a diagnosztika jelzéseire szűkítve;
- ismert eltérés: a minta saját normához mérve (csak feltárási mintánál).

Utókövetés (`models/discovery/follow_up.py`), a lezáráskor választható: „javulást várok” (újramérhető, nem
jegyszintű issue-nál; a határidőn a legutóbbi mérés `details.measured_exceeded` értéke dönt: javult, vagy nem javult →
az issue újranyílik) és „ne ismétlődjön” (bármely issue-nál; a határidőig figyeli, nyílik-e ugyanannál a
diagnosztikánál a kijelölt hatókörre – pl. csoport – új issue; ha igen: megismétlődött). Az értékelés a napi körben fut;
az eredmény az issue-n, a Javaslatok › Utókövetések listán és a tanulságok között látszik.

**Ismert hiba-fajta** (`DiscoveryService._check_known_faults`): a „valódi hiba” tanulsággal lezárt, újramérhető
issue-t a napi kör figyeli. Ha a mérése egyszer elcsendesedett (`details.known_fault_quiet_since`), majd újra
túllépést mutat, az issue magától újranyílik („ismert hiba visszatért”, `details.known_fault_returned_at`, bejegyzés a
tanulságok közé). Más tanulságú lezárás nem nyit újra.

## A jelölt három művelete

Minden jelöltnél (kártya, családtag, jelzés, naptár, elemző oldal) ugyanaz a három gomb van:

- **Feladat**: issue nyílik. A gép ebből nem tanul (`_SCOPE_WEIGHTS[None] = 0`); a tipp a lezáráskor adott
  tanulságból tanul (`confirmed` 1.5, „nem hiba” → szabály).
- **Most nem**: a jelölt félrekerül, és ha új esettel visszajön, újra a sorba kerül. A tippnek gyenge ellenpélda
  (`THIS_OCCURRENCE` súlya 0.3: nélküle a tipp csak a lezárt hibákból tanulna, és a hibás csoport zajára is
  „feladat”-ot mondana – a tanulási szimulációban 42% helyett 72% egyezés); a `Candidate.not_now` számláló minden alkalommal `not_now_boost` (3) prioritást ad, a
  kártyán „visszatérő (N×)”, 3-tól „mindig visszajön, nyiss rá feladatot”.
- **Bontsd tovább**: mintára, jelzésre és trendre is. A jelölt a Csoportos minták közé kerül; kontrasztnál a
  kontextuson belül kiugró szűkítések jönnek elő („Mi jellemző még?” sorainak visszajelzésével), és minden fajtánál az
  almérések (módszerek) futnak rajta. Almérés itt adható hozzá; mindegyikre visszajelezhető: **Jó módszer** (az
  almérés kiváltót kap az ilyen fajtájú jelöltekre ugyanannál a mérésnél, `{kind, measure}`, így magától lefut),
  **Nem jó** (`ChildDef.excluded`: ennél a fajtánál többé nem fut). Ha egy riasztó almérés a tovább bontott jelöltön
  először teljesül, a jelölt `child_alert` jelzéssel újra döntésre vár. A bontás a Tudás fülön vonható vissza.

  **A gép próbálkozásai** (`children.PROBES`, `run_probes`, `DiscoveryService._run_probes`): a bontáskor és utána
  naponta a gép maga is kipróbálja a gyári próbákat (rögtön továbbadta, rögtön lezárta, érdemi lépés nélkül, nem vették
  névre, egy csoport intézte, újranyitották, sok csoportváltás, hold, lezárási megjegyzés, elutasítva, még nyitott,
  egy napnál hosszabb) és a máshoz tanított alméréseket az érintett és a többi eseten. A legfeljebb 5 leginkább eltérő
  (legalább 15 százalékpont) kerül a családra (`stats.probes`). **Jó módszer**: a gyári próbából almérés lesz az ilyen
  fajtájú jelöltekre (`{kind, measure}`), a tanított almérés pedig ilyen fajtánál is fut. **Nem jó**: ennél a fajtánál
  a gép többé nem próbálja (`knowledge.probe_excluded`, illetve `ChildDef.excluded`).

A családokon (bontás vagy közös elem) nincs művelet, csak a tagokon. A korábbi jelölt-döntések (soha, nem dolgozik
aznap, figyeld, ismert eltérés, mind elvetése) a felületről lekerültek; a tudást adó változataik az issue lezárásakor
választhatók (nem hiba → szabály, munkaidőn kívül → tény, ismert eltérés → saját norma, más küszöb). A szerver
végpontjai megmaradtak.

**A döntés hatása.** Minden döntés, elnyomás, módszer- és bontási visszajelzés, szabály-visszavonás és issue-lezárás
után felugró ablak mondja el, mit változtat a mintakeresésen (`effect` a válaszban, `DiscoveryService._effect`): a
művelet következménye, hány további döntésre váró jelölt került ki a sorból vagy jött vissza (a döntés előtti és
utáni sor különbsége), a létrejött szabály szövege és hatóköre, csoport / egység / service elnyomásánál
figyelmeztetés. Az issue lezárásakor a hatás a tanulsággal együtt az issue-n is megmarad (`details.lesson.effect`). Az
ablak kikapcsolható („ne mutasd többé”: csak rövid értesítés marad).

## Feladat és hozzácsapás

A mintakeresés nem nyit magától issue-t: a jelöltsorban a **Feladat** gomb nyitja meg.

Amíg az issue nyitott (aktív, nem releváns vagy elavult), a mintájára illő új jelölt (ugyanaz a mérés, és a minta
szűkítése, vagy az érintett jegyeinek legalább 80%-a a findingé) nem kerül a sorba: `attached` állapotba kerül, és az
issue-hoz csatolódik (`DiscoveryManager.attach_to_open_issues`; az issue-n `details.attached`, újak:
`details.attached_news`; az issue-panel „Hozzácsatolt esetek” része, a jelöltsor fejlécében a darabszám). Ha az issue
lezárul, a csatolt jelöltek visszakerülnek döntésre (`release_attached`).

A tanulság az issue-n (`details.lesson`) és a tudástárban (`lessons`, Tudás › Tanulságok) is megmarad (a mérés
frissítése nem írja felül); a
létrejött tények és szabályok a szokásos módon felülvizsgálhatók és visszavonhatók.

## Szűkebb hatókörű küszöbök (gyári detektorok)

`models/diagnostics/core/threshold_overrides.py`. A detektor globális küszöbe csak alapértelmezés: minden mérésnél a
mért eset entitásaira (`BaseDiagnosis.entity_items`: a jelzés leírása, és egyetlen jegyet mérő detektornál a jegy
útja, jelenlegi csoportja és service-e) illő felülírás dönt. Sorrend: kézi > tanult > globális; azonos forrásnál a
szűkebb (több feltételű) hatókör nyer; paraméterenként. Ha nem a globális döntött, a mérés `details`-ében látszik:
`threshold_source` és `thresholds_used`. Létrehozás: issue lezárásakor a „más a küszöb” tanulsággal (hatókör és
értékek kiválasztva; utána újramérés), Tudás › Szűkebb küszöbök (visszavonás). A Thresholdok nézetben minden
detektor kártyáján az „Egyéni küszöbök” lista mutatja őket; ott nem írhatók át, csak törölhetők. A globális küszöb
módosítása azonnal hat és mindent újraértékel (időablakos detektornál a meglévő issue-k újramérése után a mostani
ablak teljes mérése is lefut), az egyéni küszöbök közben érvényben maradnak. Tárolás: tudástár
(`threshold_overrides`, a reset nem törli).

**Tanult küszöbök** (`models/discovery/learned_thresholds.py`, Javaslatok › Tanult küszöb-javaslatok): a jegyszintű,
mérsékelt / extrém küszöbös detektoroknál (most: ping-pong, visszanyitás) a gép service-enként, a jegy útjába eső
csoportonként és alkategóriánként nézi, hányszor lépik túl a jegyek a globális küszöböt. Ahol legalább 8 jegyből
legalább a fele túllépi, és a túllépés tartós (az első és az utolsó túllépő jegy között legalább 14 nap – egy friss
hiba ne látsszon szokásosnak), ott a túllépés a szokásos: a javaslat az ottani értékek 90. percentilise (mérsékelt), az
extrém ugyanannyival feljebb, mint a globálisnál. Az értékek elfogadás előtt átírhatók; elfogadva „tanult” forrású
szűkebb küszöb lesz, és minden újraértékelődik (a hatásablak mutatja az aktív issue-k számát előtte és utána). A „Nem
kérem” addig hallgattatja el, amíg a javasolt érték nem változik.

## A tanult tudás megbízhatósága

- **Mentés** (`models/backup.py`): a „memória” két fájl – a tudástár (`knowledge.json`: tények, almérések, küszöbök,
  bontási szempontok, munkaidő, tanulságok) és a feltárási állapot (`discovery_state.json`: szabályok, döntések,
  jelöltek). Minden mentés után, ha az előző másolat óta legalább 10 perc eltelt, a kettő együtt egy datált mappába
  kerül (`backups/`, az utolsó 30 marad), és naponta egy külön másolat is (60 nap). Reset, újraindulás és
  visszaállítás előtt mindig készül másolat. Tudás › Mentések: „Mentés most” és „Visszaállítás” (a visszaállítás
  előtti állapot is mentődik; utána minden újraértékelődik).
- **A szabályok túlélik a resetet** (`DiscoveryService._fresh_manager`): a lejátszás újraindulásakor (reset vagy az
  idő visszaugrása) a jelöltek és a döntések törlődnek, de a szabályok (elnyomások, „nem hiba” szabályok) és az
  azonosító-számlálók megmaradnak – azok tanult tudás. (Korábban a reset a szabályokat is törölte.)
- **Biztonsági szelep** (`DiscoveryService._strong_under_rules`): ha egy elvető szabály most egy nagyon erős
  (áttörő erősségű) jelöltet rejt, amely a szabály létrejötte után is megjelent, a Javaslatok › Figyelmet kér alatt piros
  tételként jön („elnyomva, de most nagyon erős”), a jelölt oldalára vivő gombbal: lehet, hogy az elnyomott helyen
  valódi hiba jött.
- **Tanulási mérleg** (`DiscoveryService.learning_balance`, Tudás › Mérleg és döntések): hetente az új jelöltek, a
  döntések (feladat / most nem – ebből visszatérő / bontás / elnyomás), a lezárt feladatok (valódi hiba / nem hiba,
  és a valódi hiba aránya), az almérés által hozott döntések és a tipp egyezése. Ha a tanulás működik, a döntések és
  a visszatérő zaj fogy, a valódi hiba aránya és a tipp egyezése nő.

## A gyári detektorok a tanuló körben

A detektorok (ping-pong, visszanyitás, csoport nélküli jegy, öregedő backlog, funkcióhasználat…) valós időben,
eseménycsomagonként mérnek, és a tanuló kör tudása rájuk is hat:

- **Leírás.** Az egyetlen jegyet mérő detektorok jelzése a jegyen túl a jegy útját is leírja: az útjába eső
  csoportok, az útvonal (`route`, pl. „L1 > A > B”), a service, az alkategória, a bejelentés módja
  (`SingleTicketDiagnosis.signal_items`, `details.entity`). Ezekre épül a „nem hiba” tanulság szabálya, a szűkebb és a
  tanult küszöb hatóköre, jóváhagyásos módban pedig a csomópont és az elnyomás.
- **Elnyomó szabály valós időben** (`models/diagnostics/core/suppression.py`): az erre a detektorra szóló aktív
  elvető szabály (a mérés-szűrőjében a detektor kulcsa – pl. egy detektor-issue „nem hiba” lezárásából, vagy egy
  jelzés elnyomásából) a detektor saját issue-nyitását is megállítja: a túllépő, a szabályra illő esetből nem nyílik
  új issue; a már nyitott issue-k mérése folytatódik. A szabályokat a feltárási szolgáltatás adja az elemzőnek
  (`IncidentAnalyzer.rule_provider` → `KnowledgeView.suppressions`). Egy mezőnek több értéke is lehet (a jegy útjába
  eső csoportok): bármelyik illeszkedése elég – ugyanígy a jelöltek szabályillesztésében (`match_rule`).
- **Ismert hiba valós időben** (`IssueManager._known_fault_returned`): a „valódi hiba” tanulsággal lezárt issue
  mérése minden csomagnál folytatódik; ha egyszer küszöb alá esett, majd újra túllép, ugyanabban a csomagban
  újranyílik („ismert hiba visszatért”). Az issue-ra írt adatok (tanulság, utókövetés, csatolt jelöltek, az ismert hiba
  jelzői) a mérés frissítésekor megmaradnak (`models.issue.KEPT_DETAIL_KEYS`).
- **Almérés.** Az öregedő backlog jelzésén csoportonként az öregedő és a csoport többi nyitott szakasza az almérés
  egysége (`BacklogAgingDiagnosis.child_units`); a funkcióhasználaté a funkció alkalmai. A jegyszintű jelzésnél
  (egyetlen jegy) almérés nem ítél, ott a csomópontok és a szabályok dolgoznak.
- **Jóváhagyásos mód** (Thresholdok › „Feladat csak jóváhagyással”): a detektor nem nyit issue-t, a jelzései a
  jelöltsorba kerülnek a fenti leírással; a funkcióhasználat alapból ilyen, a többi alapból maga nyit issue-t.

## Munkaidő

`models/working_time.py`. A szakaszos felvételi és hold idő ott, ahol a csoport munkaideje ismert, munkaórában
számít (`munkaido(csoport, napok, tol, ig)` tény → `KnowledgeView.schedules` → `build_profiles(..., schedules)`;
az adatlapon `time_basis`: munkaóra / falióra, csak megjelenítés). A munkaidő forrása:

- tanult: a Javaslatok fül munkaidő-javaslata a csoport saját (nem rendszer-) műveleteiből az elmúlt 8 hétben –
  munkanap, amelyre az aktivitás legalább 4%-a jut; az órák a legszűkebb sáv, amely az aktivitás
  `review.schedule_coverage` részét (0,75) fedi le; folyamatos, ha a sáv közel egyenletes aktivitásnyi széles és
  6–7 nap aktív. Elfogadás előtt a napok és az órák átírhatók;
- kézi: Tudás › Új tény › munkaido;
- cégszintű: `munkaido(csoport="*", …)` – minden csoportra érvényes, amelyiknek nincs saját munkaideje
  (`GroupSchedules`; most: hétfő–péntek 8–17, mert a cég hétvégén nem dolgozik). A javaslatok a csoport saját
  munkaidejéhez mérnek, így a cégszintű mellett is jönnek;
- átvett (később): a ServiceNow munkarendjei (`cmn_schedule`) ugyanebbe a tényfajtába.

Ellenőrzés: a javaslat mellett a csoport által lezárt jegyeken a mi munkaidőnk és a ServiceNow `business_stc` mezője
(lezárásig eltelt munkaidő a jegy munkarendje szerint) medián aránya; a 0,75-ös lefedés ehhez kalibrált (a valódi
adaton mediánban 1,00). A ticketszintű felvételi idő (több csoporton át) falióra marad.

## A jelöltsor szakaszai

A Tanulás › Jelöltsor három szakaszra bontja a döntésre várókat, és minden jelölt pontosan egy helyen szerepel:

- Csoportos minták: családok – egy tovább bontott szülő a gyerekeivel (alatta „Hogyan bontsuk tovább?”, „A gép
  próbálkozásai”, „Mi jellemző még?”), vagy egy közös elem (döntési csomópont) a tagjaival. A családon nincs
  művelet, csak a tagokon; amit egy család már magába foglalt, az a többiben nem ismétlődik.
- Egyedi minták: amelyek egyik családnak sem részei.
- Jelzések: a jóváhagyásos diagnosztikák jelzései (pl. funkcióhasználat).

Alul csukva a figyelés alatt álló és az ismert eltérésként kezelt jelöltek.

Kártyanézet: csak műveletes kártya van. Az egyedi minta és a jelzés kártyáján a három döntés-gomb; a családkártya
(közös elem, vagy tovább bontott szülő a döntésre váró gyerekeivel) magában mutatja a gyerekkártyákat, mindegyiken a
három gombbal. A gyerek nélküli bontás nem kártya: a szülő oldalán él munkalapként (Tudás › Bontások ›
Munkalap, vagy a családkártya „Munkalap →” gombja). Bármely kártyára vagy gyerekkártyára kattintva a jelölt saját
oldala nyílik teljes képernyőn: a kártyája a döntésekkel, tovább bontottnál a munkalap (gyerekek, almérések, a gép
próbálkozásai, mi jellemző még, bontás visszavonása). A „Bontsd tovább” után a jelölt munkalapja nyílik meg (nem új
kártya). A bontás vizsgálat, nem végső döntés: a tovább bontott jelölt oldalán a Feladat és a Most nem gomb megmarad, a gép bontásai, próbálkozásai és a hozzáadott (azonnal kiértékelt) almérések alapján itt dönthetsz. A Feladat / Most nem után a nézet visszalép. Esc / „Vissza” egy szintet lép vissza.

A kártyákon a minta egyszerűen: „Mező: érték”, a hosszú azonosító rövidítve (teljes alakja a súgóban). A jelölt
oldalán felül a **Miről szól?** doboz (`DiscoveryService.about`): a mérés neve és leírása, mikor érintett egy eset (a
küszöb), mire vonatkozik (szakasz vagy ticket), és a minta feltételei szavakban. Innen elnyomható
(`POST /api/discovery/suppress`, mind kemény szabály – `Rule.hard`: az áttörő erősségű jelöltet is rejti –, a
Tudás › Szabályok alatt visszavonható): **Ez a minta soha** (a pontos minta ennél a mérésnél), **Ezt az elemet
soha** (a mező = érték egyik mérésnél se), **az egész mezőt soha** (a mező bármely értéke, `values: ["*"]`, és a
mező keresési szerepe 0; a szabály visszavonásakor a szerep visszaáll).

Megjelenés a teljes nézetben: minden doboz összecsukható (a fejléc és a gombok összecsukva is látszanak; az állapotot a böngésző
megjegyzi; „Mind összecsuk / Mind kinyit”). A szintet szín és behúzás mutatja: 1. család vagy önálló jelölt
(türkiz), 2. tag, almérések, próbálkozások (kék), 3. a tag kártyája, értékelt módszerek (lila), 4. kártyán belüli
almérés (sárga). A tag kártyája alapból csukva. Amire műveletet indítasz (döntés, módszer-visszajelzés, bontási sor),
az azonnal eltűnik, hiba esetén visszajön. Az értékelt almérés és a „jó szempont” sor a szülőn rögzül
(`Candidate.feedback`), így újratöltés után sem kér újra döntést; az értékelt almérések a csukott „Értékelt
módszerek” alatt maradnak.

## Backlog-naptár

Tanulás › Backlog-naptár (`models/discovery/backlog.py`, `GET /api/discovery/backlog?group=&days=`): egy csoport
szakaszai napok × órák rácson – felvetlenül várakozó, nyitott vagy érkezett jegyek az óra közepén mintavételezve –, a
csoport munkaidejével keretezve. Mellette a csoportot érintő jelöltek (a csoportra szóló minták, és azok, amelyeknek
érintett esete ennél a csoportnál van); a kiválasztott jelölt kiemeli, amit időben leír (dátum, a hét napja, óra), és
pöttyökkel mutatja az érintett esetek érkezését. A kiválasztott jelöltről a naptárból dönteni is lehet.

## Tudástár a diagnosztikákban

`ctx.knowledge` (`KnowledgeView`) a mérés pillanatában érvényes tények:

- `levels`: csoport → szint (pl. melyik az L1); a `szint` tényekből,
- `availability`: (funkció, csoport) → elérhető-e; az `elerheto` / `nem_elerheto` tényekből (`*` = mindenki),
- `facts` / `of_predicate(név)`: minden más.

Diagnosztikába tudást attribútumként átadni tilos: csak a kontextusból olvasunk, így a fő elemző és
a feltárási konzol ugyanazt látja. Új tényfajta: `PredicateDef` a `models/knowledge_base.py`-ban
(`kind="classify"`: csak osztályoz; `kind="drop"`: a feltárásban elvető tudásszabály tartozik hozzá).

## A diagnosztikák

| Kulcs | Család | Ablak | Mit jelez |
|---|---|---|---|
| `PING_PONG` | Single | NONE | csoportváltások (az L1-ből való továbbítás nélkül), L1-be visszakerülés |
| `FLAPPING_INCIDENT` | Single | NONE | lezárás utáni visszanyitás |
| `UNASSIGNED_TICKET` | Single | NONE | nyitott ticket csoport nélkül (a mérés pillanatában) |
| `BACKLOG_AGING` | Cross | SNAPSHOT | csoportonként a régóta (hold nélkül) várakozó nyitott ticketek |
| `TICKET_VOLUME_DEVIATION` | Cross | TUMBLING | órás nyitási volumen a korábbi napok azonos órájához képest |
| `SIMILAR_DESCRIPTION` | Cross | SLIDING | azonos nyitáskori rövid leírás több ticketen |
| `PATTERN_FINDING` | Cross | SLIDING | elemző által elfogadott feltárási minták folyamatos újramérése |
| `FEATURE_USAGE` | Cross | SLIDING | ki milyen ServiceNow-funkciót nem használ (katalógus: `config/feature_catalog.json`) |
| `ISSUE_TREND` | Issue | SNAPSHOT | sok aktív issue egy kategóriában |

## A tanuló kör a futó rendszerben

`models/discovery/service.py` (`DiscoveryService`) köti össze a feltárást a fő elemzővel:

- a szimuláció kötegei után virtuális naponta egyszer fut (`IncidentEventProcessor` → `after_batch`),
  a dashboard „Tanulás” nézetéből kézzel is indítható;
- napvégi léptetés (Szimuláció › „Napvégéig”, `SimulationScheduler.step_day`): a lejátszás pontosan a
  virtuális éjfélnél megáll, és ott lefut a feltárás a nap végi állapotra (`on_day_end` → `discovery.run`);
  így naponta ellenőrizhető, mely félretett jelöltek (figyelés, most nem, ismert eltérés) jöttek vissza;
- a finding PATTERN_FINDING issue-ként kerül a fő adatbázisba (az elemző diagnosztikájával, a saját
  küszöbeivel mérve);
- tény felvételekor / visszavonásakor az elemző újramér (a tények a diagnosztikák eredményét alakítják);
- két jelöltforrás: a *kontrasztkereső* (`mining.py`: a minta a többi egységhez képest kiugró ugyanabban az
  időszakban) és a *saját múlt* (`trend.py`: egy entitás – csoport, service, alkategória, felelős egység,
  helyszín – a saját előző heteihez képest romlott, a populációval együtt mozgó változást kiszűrve; az
  elfogadott findingok rossz egységei kimaradnak a múltból). A kettő külön jelölt (`Candidate.kind`);
- a dimenziók szerepe a keresésben (Paraméterek › `dimensions`): 2 teljes, 1 csak szűkítő (önmagában nem minta,
  kombinációban csak ha a nélküle vett mintánál `suppressed_improvement`-szer erősebb; azonos esethalmaznál a
  szűkítő mező nélküli leírás az előnyös), 0 kizárva. Alapból az alapmezők (alkategória, hatás, bejelentés
  módja) szűkítők. Ez keresési beállítás, nem tény;
- csoport-mezők: `current_group` = a jegyhez rendelt csoport (az incidens assignment értéke a mérés
  pillanatában, csak megjelenítésre); `assignment_group` szakasznál = akinél a mintavételkor volt, ticketnél
  = a jegy útjába eső csoportok;
- döntési csomópontok (`models/discovery/clusters.py`): a jelölt *hatóköre* (döntésre váró szűkítései)
  előrébb sorolja; a közös entitás-részminták (csoport, service, alkategória, felelős egység, helyszín,
  nap) egy szabállyal egyben eldönthetők (`POST /api/discovery/decide-cluster`); a backtest és a
  hangolás a zajt ebben méri: hány elemzői *döntés* kell naponta, nem hány jelölt;
- almérések (child diagnostic, `models/discovery/children.py`): az elemző tanítja meg, mit nézzen meg a gép
  egy jelölt érintett esetein. Egy almérés: egy soros állítás szűk, biztonságos nyelven (saját értelmező,
  nincs `eval`; szókincs: `szakasz_perc`, `lezarta`, `nyit_zar_egyedul`, `ertek("mező")`, `kitoltve("mező")`
  stb.; adatsorként is megadható, `rows_to_expression`. Ki adta rá: `elozo_csoport`, `elozo_szint` (pl.
  `elozo_szint == "L1"`; a szint a `szint` tényekből jön, tény nélkül üres), `nyito_szint`. Időpontok négy
  pillanatra – `nyitas_` (a jegy nyitása), `kapta_` (a csoport megkapta), `felvette_` (névre vétel), `vege_`
  (lezárás vagy továbbítás) – mindegyiknél `_ora`, `_nap` (pl. "hétfő"), `_hetvege`, `_munkaidoben`; munkaidőben mért
  hossz: `felvetel_munkaperc`, `szakasz_munkaperc`; `munkarend_ismert`. A munkaidő a csoport ismert munkaideje
  (`nem_dolgozik`/munkaidő tények, `KnowledgeView.schedules`), ismeretlennél hivatali idő: hétköznap 8–17
  (`DEFAULT_SCHEDULE`). Az időbélyegek az adat szerintiek (időzóna nem ellenőrzött)). Két fajtája van
  (`ChildDef.method`): **ellenőrzés** (`check`, igaz/hamis állítás) és **csoportosítás** (`group`: a kifejezés
  értéke szerint, pl. `ertek("business_service")`, `elozo_csoport`, `kapta_nap`; az eredmény értékenként az
  érintettek és a többiek aránya, `groups`; teljesül, ha az érintettek legalább küszöbnyi hányada egy értékre esik;
  nem dönthet helyetted; keresési szempontként az értéke kerül az adatlapra, pl. a ráadó csoport, így a mintakeresés
  szerinte is bont). A szerep (magyarázat / riasztás) a Tudás › Almérések alatt utólag átállítható, pl. a „Jó
  módszer”-rel létrejött almérésnél. A gép próbálkozásai közt gyári csoportosítások is vannak
  (`GROUP_PROBES`: service, ráadó csoport, a megkapás napja és órája, kategória); a minta saját mezőjét bontó
  kimarad (`GROUP_PROBE_DIMENSIONS`). Egy almérésnek van egy *kiváltója* (melyik jelöltekre fusson: egy jelölt,
  egy mintaelem, pl. `feature=work_notes` vagy `weekday=Sat`, egy mérés, vagy mind), egy *szerep*
  (`explain` magyarázat / `alert` riasztás) és egy küszöb (az érintett esetek hányadánál teljesül). Az eredmény
  mindig összevetés: az érintett esetek és az összehasonlító esetek (mintánál a többi egység, jelzésnél
  ugyanez a funkció a többi csoportnál, `BaseDiagnosis.child_units`) hányadára igaz. A napi körben a kiváltóra
  illő döntésre váró jelölteken magától lefut (`_run_children`); a teljesült magyarázat nem-finding, a
  riasztás finding tippet ad (és ezzel a prioritást is mozgatja). Az almérések a tudástárban élnek (a reset
  nem törli), döntéskor számolódik, hányszor egyezett az elemző a jelzésükkel (Tudás › Almérések).
  *Keresési szempont* (`as_dimension`): az almérés igen/nem értéke `almeres:<azonosító>` dimenzióként
  kerül minden adatlapra (`KnowledgeView.derived` → `build_profiles(..., derived)`; a feltárás, az elemzés,
  a visszamenőleges darabszámok és a PATTERN_FINDING ugyanígy számol), így a mintakeresés és a
  „Bontsd tovább” az elemző logikája szerint is bont. Felülvizsgálat (`review_children`, Javaslatok fül):
  félrevisz (a döntések ritkán egyeznek a jelzésével), sosem teljesül, sosem fut; a megerősítés a
  statisztikát nullázza a felülvizsgálat számára (`reviewed_stats`).
  *Automatikus döntés* (`auto`, csak magyarázatnál): ha teljesül, „most nem” döntés születik az elemző
  helyett (`Decision.auto`); nem fogyaszt keretet, nem tanítja a tippet, és nem számít az almérés saját
  egyezésébe. Visszatéréskor a kártya az előző arányt is mutatja (`previous_share`).
  Jelzés figyelése: új érintett esetnél korán visszajön („megismétlődött”), lejáratkor „még fennáll”, ha
  még jelez, különben „nem ismétlődött”; visszatéréskor az almérések újra lefutnak.
  API: `GET /api/discovery/children`, `POST /api/discovery/children` (`op`: test | add | attach | revoke |
  confirm | dimension | auto);
- „Bontsd tovább” (`Verdict.REFINE`, állapot `refined`): a minta önmagában nem finding (pl. szombaton lassabb a
  felvétel), de nem is elvetett vagy figyelt. *Kontextus* lesz: maga nem jön vissza a sorba, a bányászat
  (`mine(..., contexts)`) viszont a szűkítéseit (pl. szombat & X csoport) már a kontextus arányához méri, nem
  az átlaghoz, és ezek külön FDR-családot alkotnak. A jelölt kártyája mutatja, hányszorosa a kontextusnak
  (`context`, `context_rate`, `context_lift`). Nem tanuló döntés; a Tudás › „Tovább bontott minták” alatt
  visszavonható (`POST /api/discovery/unrefine`). A Jelöltsor „Bontások” blokkja a szülőt a gyerekeivel
  együtt mutatja: a leíró bontást (`analysis.breakdown`: mi gyakoribb a szülő érintett eseteiben, mint a
  szülő összes esetében – statisztikai szűrő nélkül, naponta és a döntéskor frissül), az önálló jelöltté vált
  szűkítéseket (a szülőhöz mért kiemeléssel és lefedettséggel) és a megmagyarázatlan maradékot.
  A bontás soraira visszajelzés adható (`POST /api/discovery/breakdown`): **„jó szempont”** – a módszerről szól,
  nem az értékről: ennél a mérésnél a gép ezt a mezőt minden jelöltnél megnézi (`breakdown.measures`,
  `DiscoveryManager.good_dimensions`; a napi körben és a visszajelzéskor `stats.good_dims` – a kártyán „Bevált
  szempontok”: mezőnként az érintettek leggyakoribb értéke és aránya a minta összes esetéhez képest), és a bontási
  táblákban ennél a mérésnél előrébb kerül; az értékből nem lesz jelölt. „zsákutca” – ennél a bontásnál a sor
  rejtve; „soha” – a mező egyik bontásban sem (és bevált szempont sem lehet). A tanult szempontok a tudástárban
  élnek (a reset nem törli; Tudás › Bontási szempontok, mérésenként);
- általánosítási javaslatok (`models/discovery/generalize.py`, Javaslatok fül): amit az elemző ismételten
  ugyanúgy döntött el, azt a gép felajánlja általános tudásnak – ugyanaz a mezőérték több elvetési szabályban
  (→ általános szabály), ugyanaz a tulajdonság-mező sok értékkel (→ csak szűkítő szerep; entitás-mezőkre nincs
  ilyen javaslat), ugyanaz a mező több bontásnál zsákutca (→ soha), többször jó szempont (→ kiemelt), és a
  *bontási út*: a szülő mezőfajtájából (`mining.dimension_class`: a hét napja és a dátum „nap”, a csoportmezők
  „csoport”) a bontó mezőbe vezető út több különböző szülőnél zsákutca (pl. nap → alkategória) → az út tiltott
  bármely értékre és csoportra (`MiningParams.blocked_paths`: a „Bontsd tovább” gyerekei közt sem). Csak elfogadásra
  hat; az elutasított javaslat csak küszöbnyi új bizonyítékkal jön vissza (`review.generalize_min_count`, alapból 3);
- a dashboard API-ja: `GET /api/discovery`, `/api/discovery/candidates/<id>`, `/knowledge`, `/review`,
  `/config`; `POST /api/discovery/run`, `/decide`, `/fact`, `/rule`, `/availability`, `/config`.

A konzol (`discovery_cli.py`) önállóan, egy idősor-fájlon dolgozik (kísérletezéshez); a tudástárat
ugyanúgy a `knowledge.json`-ból olvassa.

## Elemzés a jelölt oldalán (döntés előtt)

A jelölt saját oldalán (Tanulás › kártya, közvetlen link: `/#candidate=<id>`; a régi `/analysis?candidate=<id>` ide
irányít át) a „Miről szól?” és a döntés-kártya alatt az **Elemzés** szakasz (adat: `GET
/api/discovery/candidates/<id>/analysis`, számítás: `models/discovery/analysis.py`; a felület `analysisSection` a
`dashboard.html`-ben, összecsukható részekkel). Ugyanabból az adatlap-rétegből dolgozik, mint a feltárás, a jelölt
mérési pillanatában:

- magyarázat: a mérés, a rossz-küszöb és a minta feltételei szavakban; a fő számok;
- „mi köti még össze a rossz eseteket”: dimenziónként a rosszak leggyakoribb értéke vs. a populáció
  aránya (a kiemelt sor a valódi közös pont; csoport + nap esetén egy kattintással `nem_dolgozik` tény);
- napi rossz arány (minta vs. populáció) és a mért érték eloszlása, táblázatnézettel;
- esetek: a mintán belüli és kívüli ticketek/szakaszok a mért értékkel és a mérést meghatározó
  eseményekkel; szűrés, rendezés; a kijelöltek history-ja egymás mellett (összevetés);
- a döntés a fenti kártyán; tovább bontott jelöltnél alatta a bontás munkalapja.

A kártyára kattintva, a kártya „Oldala →” gombjával, és az issue-panelről („Jelölt oldala ↗”) nyílik. Jelzésnél
(jóváhagyásos detektor) az Elemzés szakasz nincs: ott a „Miről szól?”, az almérések és a próbálkozások adják a képet.

## Szintetikus adat és backtest

- `build_synthetic.py`: valódi incidensek újramintavételezésével előtörténet a valódi idősor elé;
  `--faults default|<fájl>` hibákat ültet be (`synthetic/faults.py`: slow_pickup, ping_pong, long_hold,
  drop_feature; step vagy ramp), a ground truth a `.faults.json`-ban.
- `backtest.py`: napról napra visszajátssza a feltárást és a diagnosztikákat, és forgatókönyvenként méri a
  felismerés késését, a hiba előtti „természetes” jelzést és a zajt. A paraméterek hangolásának mérőpadja.
- `learning_demo.py` (`synthetic/learning_sim.py`): tanítási szimuláció – a teljes tanuló kör napról napra,
  szimulált elemzővel (a megoldókulcs és a valódi elemző döntési stílusa szerint: finding, Bontsd tovább,
  Most nem, Erre soha, zaj-csomópont egyben, heti általánosítás), és mellette egy tanulás nélküli kör ugyanazon
  az adaton. Eredmény: `learning_demo.json` (napi döntések, sor, keret, szabályok, tippegyezés; a hibák útja).
- A lejátszó forrása választható (`timeline_sources.py`, Szimuláció › Adatforrás): valódi idősor,
  szintetikus előtörténet beültetett hibákkal (`synthetic_timeline.json`), vagy ugyanez hibák nélkül
  (`.clean.json`). A váltás resetel (a tudástár marad); a szintetikus fájlt a reset nem törli. A beültetett
  hibák listája (megoldókulcs) a panelen lenyitható. Legyártás: `python build_synthetic.py --faults default --with-clean`.

## Új diagnosztika – ellenőrzőlista

1. Családválasztás: egy ticket útja (Single), ticketek csoportja (Cross), issue-k (Issue).
2. A mérés a `models/timeline.py`-ból, `ctx.measured_at`-ig. Csoportra vonatkozó mérésnél szakaszonként.
3. `threshold_definitions()` (és időablaknál `window_definitions()`): minden küszöb paraméter, leírással
   és mértékegységgel – a dashboard ebből rajzolja a paraméterezőt.
4. `classify` csak a details-ből dönt (az újramérés és a paraméterváltozás is ezt hívja).
5. Bizonyíték: `affected` és `history_refs` (`make_history_ref`) – az issue-ból a history sorig vissza kell tudni menni.
6. Tudás csak `ctx.knowledge`-ből.
7. Regisztrálás az `analyzer.py`-ban; teszt a `tests/` alatt (ld. `tests/test_core_diagnostics.py`).
