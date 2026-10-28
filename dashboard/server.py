from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

from config.paths import DATA_DIR
from dashboard.dashboard_service import DashboardService
from dashboard.request_handler import DashboardHandler


class _DashboardHTTPServer(ThreadingHTTPServer):
    """A böngésző induláskor egyszerre több kérést nyit; a lassabb kérések (pl. a backlog-naptár) mellett az
    alapértelmezett 5-ös várakozási sor betelik, és a kapcsolatok eldobódnak ("Failed to fetch")."""

    request_queue_size = 64
    daemon_threads = True

def start_dashboard_server(
    database_path: str = str(DATA_DIR / "database.json"),
    dashboard_path: str = "dashboard.html",
    host: str = "127.0.0.1",
    port: int = 8765,
    scheduler=None,
    repository=None,
    analyzer=None,
    discovery=None,
):
    service = DashboardService(database_path, scheduler, repository, analyzer, discovery)
    handler = type("ConfiguredDashboardHandler", (DashboardHandler,), {})
    handler.service = service
    handler.dashboard_path = Path(dashboard_path)
    server = _DashboardHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
