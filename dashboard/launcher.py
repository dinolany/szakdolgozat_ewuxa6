import webbrowser

DASHBOARD_URL = "http://127.0.0.1:8765/"


def open_dashboard(url: str = DASHBOARD_URL) -> None:
    """A dashboard megnyitása a böngészőben (a szerver a dashboard/static/dashboard.html-t szolgálja ki).

    Hívja: main.refresh_dashboard."""
    webbrowser.open(url)
