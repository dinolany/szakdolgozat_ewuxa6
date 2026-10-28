import threading
import traceback

from config.paths import DASHBOARD_HTML, DATA_DIR
from dashboard.launcher import open_dashboard
from dashboard.server import start_dashboard_server
from engine.analyzer import IncidentAnalyzer
from engine.event_processor import IncidentEventProcessor
from learning.discovery_service import DiscoveryService
from simulation.playback_queue import PlaybackQueue
from simulation.simulation_scheduler import SimulationScheduler
from simulation.time_transformer import TimeTransformer
from simulation.timeline_selector import TimelineSelector
from storage.excel_loader import excel_fingerprint, load_excel
from storage.incident_repository import IncidentRepository


# Repository példányosítása a database.json fájlra
repo = IncidentRepository("database.json")

def refresh_dashboard(auto_open: bool = False):
    """Az adatbázis állapotának kiírása, és kérésre a dashboard megnyitása a böngészőben."""
    try:
        count = repo.count()
        if count == 0:
            print("\n[INFO] A database.json jelenleg üres.")
        else:
            print(f"\n[DASHBOARD] {count} db incidens az adatbázisban.")
        if auto_open:
            open_dashboard()
    except Exception as e:
        print(f"\n❌ [HIBA] A dashboard megnyitása meghiúsult: {e}")
        traceback.print_exc()

def trigger_dashboard_async(auto_open: bool = False):
    thread = threading.Thread(
        target=refresh_dashboard, args=(auto_open,), daemon=True
    )
    thread.start()


if __name__ == "__main__":
    # 1. Komponensek tiszta példányosítása
    # A transzformer csak üres vagy elavult timeline cache esetén kéri el a nyers adatot a loadertől
    transformer = TimeTransformer(
        raw_source=load_excel,
        output_filename="transformed_timeline.json",
        source_fingerprint=excel_fingerprint,
    )
    playback_queue = PlaybackQueue()
    # A lejátszás forrása választható: valódi idősor vagy a build_synthetic.py szintetikus idősora
    timeline_source = TimelineSelector(transformer)

    # Eseményfeldolgozás: a csomagokat incidensekre alkalmazza, elemez, és a repóval ment
    analyzer = IncidentAnalyzer(thresholds_path="database.json.thresholds.json")
    # A feltárási ("tanuló") kör: a kötegek után naponta fut, a dashboardról vezérelhető
    discovery = DiscoveryService(analyzer, repo)
    event_processor = IncidentEventProcessor(repository=repo, analyzer=analyzer, discovery=discovery)

    # A scheduler csak vezérel: adatot a transzformertől kér, a csomagokat a feldolgozónak adja,
    # törlésnél a repót és a transzformert kéri meg
    scheduler = SimulationScheduler(
        transformer=timeline_source,
        playback_queue=playback_queue,
        event_processor=event_processor,
        repository=repo,
        on_reset=discovery.reset,  # a feltárás állapota is törlődik (a tudástár marad)
        on_day_end=lambda day_end: discovery.run(day_end),   # napvégi léptetésnél: feltárás a nap végi állapotra
    )

    # 2. Dashboard szerver elindítása a schedulerrel
    dashboard_server = start_dashboard_server(
        database_path=str(DATA_DIR / "database.json"),
        dashboard_path=str(DASHBOARD_HTML),
        scheduler=scheduler,
        repository=repo,  # ugyanazok a példányok, mint az eseményfeldolgozóé
        analyzer=analyzer,
        discovery=discovery,
    )

    try:
        print("\n================ VEZÉRLŐ PANEL ================")
        print(" [run]    Szimuláció indítása (scheduler.start) és database.json feltöltése")
        print(" [e]      Dashboard megnyitása böngészőben")
        print(" [r]      Rendszer alaphelyzetbe állítása (scheduler.reset)")
        print(" [q]      Kilépés")
        print("================================================\n")

        while True:
            cmd = input("Parancs [run / e / r / q]: ").strip().lower()

            if cmd == "q":
                break

            # Egy parancs hibája nem állíthatja le a programot (és vele a dashboard szervert)
            try:
                if cmd == "run":
                    print("--> [SYSTEM] Szimuláció indítása a scheduleren keresztül...")
                    # duration_seconds=0: minden esemény azonnal, egyben feldolgozva
                    status = scheduler.start(duration_seconds=0)
                    if status["state"] == "error":
                        print(f"❌ [SYSTEM] A szimuláció hibával állt le: {status['error']}")
                    else:
                        trigger_dashboard_async(auto_open=False)
                        print(f"--> [SYSTEM] Futás kész ({status['processed']}/{status['total']} esemény). Ellenőrizd a database.json-t!")
                elif cmd == "e":
                    trigger_dashboard_async(auto_open=True)
                elif cmd == "r":
                    scheduler.reset()
                    print("--> [SYSTEM] Scheduler és Database alaphelyzetbe állítva.")
                else:
                    print("Érvénytelen parancs! Választható: 'run', 'e', 'r', 'q'")
            except Exception as error:
                print(f"❌ [SYSTEM] A(z) '{cmd}' parancs sikertelen: {error}")
                traceback.print_exc()

    except KeyboardInterrupt:
        print("\nA leállítás kérése fogadva.")
    finally:
        dashboard_server.shutdown()
        dashboard_server.server_close()
        print("A szolgáltatások leálltak. Kilépés.")