import os
import sys
import threading
import traceback

from dashboard_generator import generate_dashboard, reset_dashboard
from analyzer import IncidentAnalyzer
from dashboard_server import start_dashboard_server
from event_processor import IncidentEventProcessor
from models.discovery.service import DiscoveryService
from timeline_sources import TimelineSelector
from loader import excel_fingerprint, load_excel
from repository import IncidentRepository
from time_transformer import TimeTransformer
from playback_queue import PlaybackQueue
from simulation_scheduler import SimulationScheduler

# Repository példányosítása a database.json fájlra
repo = IncidentRepository("database.json")

def refresh_dashboard(auto_open: bool = False):
    """Generálja a Dashboard HTML-t a database.json alapján."""
    try:
        count = repo.count()
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, "database.json")
        html_path = os.path.join(base_dir, "dashboard.html")

        if count == 0:
            if auto_open:
                reset_dashboard(
                    json_path=json_path,
                    output_html=html_path,
                    open_browser=auto_open,
                )
            print("\n[INFO] A database.json jelenleg üres.")
            return

        print(f"\n🛠 [DASHBOARD] HTML generálás indítása... ({count} db Incidens)")
        generate_dashboard(
            json_path=json_path,
            output_html=html_path,
            open_browser=auto_open,
        )
        print(f"\n[DATABASE EXPORT] Frissítve -> '{html_path}' ({count} db Incidens)")

    except Exception as e:
        print(f"\n❌ [HIBA] A Dashboard generálása meghiúsult: {e}")
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
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dashboard_server = start_dashboard_server(
        database_path=os.path.join(base_dir, "database.json"),
        dashboard_path=os.path.join(base_dir, "dashboard.html"),
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