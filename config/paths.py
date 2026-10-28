"""A projekt mappái egy helyen: a modulok ehhez viszonyítanak, nem a saját helyükhöz."""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"          # adatbázis, tudástár, tanulási állapot, idősorok, mentések
DASHBOARD_HTML = PROJECT_ROOT / "dashboard" / "static" / "dashboard.html"
