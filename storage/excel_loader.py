# loader.py
import os
import pandas as pd
from typing import Any, Dict, List

from config.paths import DATA_DIR


def excel_fingerprint(excel_path: str = str(DATA_DIR / "data.xlsx")) -> str | None:
    """Az Excel aktuális állapotának azonosítója (méret + módosítási idő); None, ha nincs meg a fájl."""
    try:
        stat = os.stat(excel_path)
    except OSError:
        return None
    return f"{os.path.abspath(excel_path)}|{stat.st_size}|{stat.st_mtime_ns}"


def load_excel(excel_path: str = str(DATA_DIR / "data.xlsx"), sheet_name: str = "inc_history") -> List[Dict[str, Any]]:
    """Kizárólag az audit log Excel beolvasása nyers rekordlistává (transzformáció nélkül)."""
    if not os.path.exists(excel_path):
        raise FileNotFoundError(
            f"A megadott Excel fájl nem található: {excel_path}"
        )

    df = pd.read_excel(excel_path, sheet_name=sheet_name)

    # KÖTELEZŐ: Oszlopnevek megtisztítása a rejtett szóközöktől
    df.columns = [str(c).strip() for c in df.columns]

    time_col = next((c for c in ["sys_created_on", "created_at", "sys_updated_on"] if c in df.columns), None)
    if time_col:
        df["created_at"] = pd.to_datetime(
            df[time_col],
            format="%d.%m.%Y %H:%M:%S",
            dayfirst=True,
            errors="coerce",
        )

    # NaN / NaT -> None
    df = df.astype(object).where(pd.notnull(df), None)

    records = df.to_dict(orient="records")
    print(f"[LOADER] {len(records)} sor beolvasva: '{excel_path}' ({sheet_name})")
    return records
