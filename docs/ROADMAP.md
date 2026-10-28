# Útiterv: a tanuló modul és a fix diagnosztikák egyesítése

Állapot: 2026-10-21. A részletes működés: `docs/DIAGNOSTICS.md`. Ez a fájl a megbeszélt irányt és a sorrendet rögzíti,
hogy egy új munkamenet is ugyanonnan folytathassa.

## Ahol most tartunk

- **Két külön világ fut.**
  - Fix diagnosztikák (ping-pong, flapping, backlog-öregedés, felelős nélküli jegy, funkcióhasználat…):
    eseménycsomagonként mérnek, kézzel beállított küszöbbel, és maguk nyitnak issue-t.
  - Tanuló kör (mintakeresés, trend, jelzések, almérések, tipp, jelöltsor): naponta egyszer fut (napváltás után,
    „Napvégéig” léptetésnél éjfélkor, vagy kézzel).
  - A kettő közti kapocs ma csak ennyi:
    - a jóváhagyásos diagnosztikák (most: FEATURE_USAGE) jelzései a jelöltsorba mennek;
    - bármely issue lezárható tanulsággal, amely tényt vagy szabályt hoz létre (`models/discovery/lessons.py`);
    - az elfogadott findingot a PATTERN_FINDING diagnosztika csomagonként újraméri.
- **Megvan a tudás minden fajtája:** tény, szabály, almérés, bontási út és szempont, munkaidő, ismert eltérés normája,
  általánosítási javaslat, felülvizsgálat.
- **A felület túl sok fogalmat mutat külön gombként.** Kb. 15 döntésfajta van 6 helyen és 7 fülön.

## A cél

1. **A küszöb rugalmas legyen:** a beállított küszöb csak alapértelmezés. „Használd a beállított küszöböt, HA NEM
   TANULTUNK JOBBAT.” Feloldási sorrend egy mérésre (diagnosztika × entitás, pl. csoport):
   1. az elemző által kézzel adott, szűkebb hatókörű küszöb (csoportra, service-re, útvonalra);
   2. tanult norma (ismert eltérés / saját alapvonal, munkaidővel, akár óránként; jóváhagyással);
   3. a diagnosztika globális küszöbe (ma a Thresholdok nézet).
2. **Az issue-t a program a tanultak alapján példányosítsa,** és minden issue-nak legyen visszacsatolása a tudásba (a
   lezárás tanulsággal már megvan).
3. **Tanítás és alkalmazás szétválasztása:** új mintát naponta egyszer tanul, az ismert mintákat folyamatosan
   figyeli („őrök”, csomagonként). Riasztási csillapítás (tartós eltérés) és összevonás (jelenségenként egy issue).
4. **Egyszerűbb felület:**
   - kétlépéses döntés mindenhol: „Mi ez?” (hiba / nem hiba / még nem tudom) + „Mit tanuljon belőle?” (a helyzethez
     illő 2–4 lehetőség, egy javasolt);
   - egy „Javaslatok” postafiók (általánosítás, munkaidő, felülvizsgálat, kérdések);
   - egy „Tudás” lista fajta-szűrővel;
   - fülek: Jelöltsor, Naptár, Javaslatok, Tudás (+ Paraméterek).

## Két egyenrangú jelöltforrás: általános mintakeresés és gyári detektorok

A régi diagnosztikák nem lógnak a levegőben: ezek **gyári detektorok**, előre ismert anomália-fajták a saját mérési
módszerükkel és saját paraméterezéssel. A mintakeresés ismeretlen mező-összefüggéseket keres, a detektorok ismert
fajtákat mérnek. Mindkettő ugyanabba a jelöltsorba ad, mindkettőre ugyanazok a döntések hatnak, és mindkettő tanul:
a detektor a gyári küszöbét csak addig használja, amíg nem tanult jobbat (entitásonként: csoport, útvonal, service).

| Detektor | Mit mér (gyári) | Mit tanulhat |
|---|---|---|
| PING_PONG | csoportváltások száma jegyenként | útvonalanként / service-enként szokásos váltásszám; elfogadott útvonalak |
| FLAPPING_INCIDENT | lezárás utáni visszanyitás | csoportonként szokásos visszanyitás; természetes újranyitási okok |
| BACKLOG_AGING | nyitott jegy kora | csoportonként szokásos kor, munkaórában |
| UNASSIGNED_TICKET | felelős / csoport nélkül töltött idő | csoportonként szokásos, munkaórában (átfed a szakaszos felvétellel) |
| TICKET_VOLUME_DEVIATION | érkezési volumen eltérése | service × óra / nap szokásos volumene (szezonalitás) |
| SIMILAR_DESCRIPTION | hasonló leírású jegyek csoportja (tömeges hiba) | zajos, mindig ismétlődő szövegek kiszűrése |
| FEATURE_USAGE | funkcióhasználat (már a tanuló körben) | elérhetőség, „nem elvárt” tények, almérések |
| PATTERN_FINDING | elfogadott minta újramérése | → folyamatos „őr” lesz |
| ISSUE_TREND | aktív issue-k trendje (meta) | meta-szint marad: az issue-kból számol |

Minden detektornál a jelzésnek olyan leírást kell adnia (csoport, útvonal, service, nap, óra), amire a szabályok, a
tények, a normák, a csomópontok és az almérések ugyanúgy építhetnek, mint a mintakeresés jelöltjeire.

## Futási modell (megállapodás, 2026-10-21)

- Gyári detektorok (a `models/diagnostics` package osztályai): valós időben, minden eseménycsomaggal futnak, a feloldott
  küszöbükkel, és issue-t nyitnak (vagy jóváhagyásos módban jelzést adnak).
- Mintakeresés: tanuló módban marad, naponta egyszer; a jelöltjei az elemző elé kerülnek.
- Ami a mintákból alapmérésnek való:
  1. új mérési logika kell → új osztály a diagnostics package-ben (DIAGNOSTICS.md, „Új diagnosztika – ellenőrzőlista”);
  2. csak a meglévő mérések egy mintája → nem kell kód: az elfogadott mintából „őr” lesz (általános, a minta által
     paraméterezett detektor), amely ugyanúgy valós időben fut (1b lépés).

## Haladás

- 2026-10-21 – 1a kész: küszöb-feloldás a gyári detektorokban (kézi > tanult > globális, entitásonként), „más a
  küszöb” tanulság issue lezárásakor, Tudás › Szűkebb küszöbök. A sorrendet a megbeszélés után módosítottuk: a
  kézzel megadott szűkebb küszöb nyer a tanult felett (az elemző kifejezett döntése az erősebb).
- 2026-10-21 – utókövetés a lezárt issue-kra (javulást várok / ne ismétlődjön), napi értékeléssel; a nem javult
  issue újranyílik. Ez az őrök első fajtája.
- 2026-10-22 – „Feladat” gomb (issue nyitása mintából, gyenge címke), hozzácsapás (a nyitott issue mintájára illő új
  jelölt az issue-hoz kerül, nem a sorba), ismert hiba-fajta (a „valódi hiba” tanulsággal lezárt issue elcsendesedés
  után visszatérve újranyílik). Egyelőre a napi körben fut; a csomagonkénti futás az őrökkel jön.
- 2026-10-22 – a jelöltnek három művelete van: Feladat (nem tanít; a lezárás tanít), Most nem (nem tanít; a
  visszatérő egyre előrébb), Bontsd tovább (a vizsgálat helye: gépi bontás és almérések, „jó módszer / nem jó”
  visszajelzéssel, amely általánosítja a módszert). Családon nincs művelet. A tudást adó döntések az issue lezárásához
  kerültek.
- 2026-10-22 – „Bontsd tovább” után a gép is próbálkozik: gyári próbák és máshoz tanított almérések, a leginkább
  eltérők felajánlva; jó módszer → almérés az ilyen fajtájú jelöltekre, nem jó → ennél a fajtánál többé nem próbálja.
- 2026-10-22–27 – felület: kártyarács (minden kártyán művelet, a családkártya a gyerekeivel), a jelölt saját oldala
  („Miről szól?”, döntés, bontás munkalap), csoportosító almérés, időváltozók (munkaidő, hétvége, ráadó csoport),
  elnyomás a jelölt oldaláról (minta / elem / egész mező, kemény szabály). A napi keret jelöltenként számol.
- 2026-10-27 – a tanulási szimuláció az új menettel (`synthetic/learning_sim.py`): 237 döntés a tanulás nélküli
  454 helyett (−48%; a régi menet csomópont-döntésekkel 169 volt), mind a 4 egycsoportos hiba ugyanazon a napon lett
  feladat, a rendszerszintű lassulásra nincs téves csoportriasztás. A tipp egyezése a végére 42%-ra romlik, mert
  csak a lezárás és a „soha” tanítja (ellenpélda alig van); próbaként a „most nem” 0,3-as súlyú ellenpéldával 72%
  (döntésszám változatlan) – döntés kell róla.
- 2026-10-27 – a felugró hatásablak (minden döntés és issue-lezárás után), a „most nem” gyenge (0,3) ellenpélda a
  tippben, L1 szint-tény, cégszintű munkaidő (`munkaido("*")`, hétfő–péntek 8–17), felvételi küszöb 8 munkaóra.
- 2026-10-27 – a gyári detektorok a tanuló körben (a tanuló modul ezzel kész):
  - a jegyszintű detektorok jelzése leírja a jegy útját (csoportok, útvonal, service, alkategória, bejelentés módja);
  - a „nem hiba” tanulság / elnyomás szabálya a detektor valós idejű issue-nyitását is megállítja (valós adaton: egy
    csoportra szóló ping-pong szabály után a csoporton átmenő 16 issue egyike sem nyílt újra, a többi 36-ból 34 igen);
  - több értékű mezők a szabályillesztésben (a jegy útjába eső bármelyik csoport);
  - ismert hiba valós időben (minden csomagnál); javítva, hogy a mérés frissítése törölte a csatolt jelölteket és az
    ismert hiba jelzőit;
  - tanult küszöb-javaslatok (ping-pong, visszanyitás; service / csoport / alkategória szerint, tartós túllépésnél,
    jóváhagyással) – valós adaton 2 javaslat, az egyik elfogadva 48 → 41 aktív ping-pong issue;
  - almérés az öregedő backlogon; csoportosító almérés keresési szempontként; az almérés szerepe átállítható.
- 2026-10-27 – az elemző oldal a jelölt oldalába olvadt; a bontásnál „jó szempont” (a mérésnél minden jelöltnél
  megnézi a mezőt, nem az értékből jelölt); megbízhatóság: a tudás mentése és visszaállítása, a szabályok túlélik a
  resetet (eddig elvesztek), biztonsági szelep az elnyomásokra, heti tanulási mérleg.
- 2026-10-27 – takarítás és kipofozás:
  - kivezetve: a „Figyeld” (állapot, döntés, lejárat; a „most nem” váltja ki), a csomópont-döntés (Mind elvetése /
    Mind figyelése), a régi döntésfajták a döntés-végpontból (soha, nem dolgozik aznap, ismert eltérés – az
    elnyomás-gombok és a lezárási tanulság adják), a bontásból-jelölt, a halott felületi kód;
  - egységesítve: egy szabályillesztés (`conditions_match`) a jelöltekre és a detektorokra, egy „aktív jelöltek”
    lista, egy adatlap-építés és bontás-számolás, egy mezőnév-forrás, egy százalékformázó;
  - a tanuló modulok minden metódusa leírást kapott a feladatáról és a hívóiról („Hívja:”);
  - a fülek: Jelöltsor · Backlog-naptár · Javaslatok (kérdések, tanult küszöbök, munkaidő, általánosítás,
    felülvizsgálat, utókövetés; a figyelmet kérő elöl, számlálóval) · Tudás (fajta-szűrővel; a mérleg és a döntések is)
    · Paraméterek.
- Következik: a dokumentáció átírása (felhasználói és fejlesztői leírás). A tanuló modul
  igazi próbája egy több hónapos valódi ServiceNow-kivonat lesz.

## Sorrend (megbeszélt)

1. **Küszöb-feloldás és őrök:** a fix diagnosztikák a fenti sorrendben oldják fel a küszöbüket. Az elfogadott minta,
   az ismert eltérés, a figyelés és a munkaidős backlog folyamatos őrként fut, csillapítással, és issue-t nyit.
2. **Diagnosztikák a tanuló körbe:** ping-pong, flapping és a többi küszöbös mérés jóváhagyásos módban, gazdagabb
   jelzéssel (útvonal, service), hogy a szabályok, normák és almérések rájuk is hassanak.
3. **Felület összevonása:** kétlépéses döntés („Mit tanuljon ebből?” mint mag), Javaslatok, Tudás.
4. Később: valódi adatbázis a JSON helyett (vállalati környezetben); ServiceNow `task_sla` és `cmn_schedule` átvétele
   (az SLA kész eredményét átvenni, nem újraszámolni; a munkarend a tanult munkaidő helyére).

## Nyitott kérdések

Lezárva (2026-10-27):
- Az L1 a 07b30f6f… csoport (`szint` tény). Hogy hétvégén alig van L1-adat, az valós: a cég hétvégén nem dolgozik,
  csak súlyos esetben van bejelentés. Cégszintű munkaidő: hétfő–péntek 8–17 (`munkaido("*")`), a csoport saját
  munkaideje felülírja.
- close_code: nem használjuk, a mért számok elegendők.
- A szakaszos felvételi küszöb 8 munkaóra lett (16 falióra helyett). A valódi adaton 53 helyett 46 érintett szakasz
  (8,9%); a korábbi érintettek közül az L1 éjszakai / hétvégi várakozásai kiesnek.
