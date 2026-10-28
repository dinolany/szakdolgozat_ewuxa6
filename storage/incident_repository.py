from contextlib import contextmanager
from enum import Enum
import json
import os
import shutil
import threading
import time
from typing import Any, Dict, Iterator, Optional

from model.incident import Incident
from model.issue import Issue
from storage.errors import DatabaseCorruptedError, DatabaseIOError
from storage.json_cleaning import clean_nan


class IncidentRepository:
    """A database.json kizárólagos kezelője: betöltés, mentés, ürítés, zárolás.

    Üzleti logikát nem tartalmaz; a módosításokat a hívók a transaction() blokkjában végzik.
    """

    def __init__(self, db_file: str = "database.json"):
        self.db_file = db_file
        # Újrabelépő zár: egy tranzakción belül ugyanaz a szál további repository hívásokat tehet
        self._write_lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """Az adatbázis írási zárja (pl. ha egy beállítás változása nem futhat elemzés közben)."""
        return self._write_lock

    @contextmanager
    def transaction(self) -> Iterator[Dict[str, Any]]:
        """Zár alatt egyszer betölt, a blokk végén egyszer ment.

        Ha a blokkban kivétel keletkezik, nincs mentés (a fájl változatlan marad), a kivétel továbbmegy.
        """
        with self._write_lock:
            db_data = self._load_db()
            yield db_data
            self._save_db(db_data)

    def _get_inc_id(self, incident: Any) -> str:
        if isinstance(incident, dict):
            return str(incident.get("id") or incident.get("incident_id") or incident.get("number") or "UNKNOWN")
        return str(getattr(incident, "id", None) or getattr(incident, "incident_id", None) or getattr(incident, "number", "UNKNOWN"))

    READ_RETRIES = 5
    REPLACE_RETRIES = 10
    RETRY_DELAY = 0.1

    @staticmethod
    def _empty_db() -> Dict[str, Any]:
        return {"total_incidents": 0, "incidents": {}, "issues": {}, "issue_history": []}

    def _load_db(self) -> Dict[str, Any]:
        """Betölti az adatbázist. Hiányzó vagy üres fájl = üres adatbázis.

        Sérült fájl esetén NEM ad vissza üres adatbázist (az a következő mentéssel felülírná az adatot),
        hanem félreteszi egy másolatát és DatabaseCorruptedError-t dob.
        """
        if not os.path.exists(self.db_file):
            return self._empty_db()

        content = self._read_text_with_retry()
        if not content.strip():
            return self._empty_db()

        try:
            data = json.loads(content)
            if not isinstance(data, dict):
                raise ValueError("a gyökérelem nem JSON objektum")
            data.setdefault("issues", {})
            data.setdefault("incidents", {})
            data.setdefault("issue_history", [])
            data["incidents"] = {
                incident_id: incident if isinstance(incident, Incident) else Incident.from_dict(incident)
                for incident_id, incident in data["incidents"].items()
            }
            data["issues"] = {
                issue_id: issue if isinstance(issue, Issue) else Issue.from_dict(issue)
                for issue_id, issue in data["issues"].items()
            }
            return data
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            backup = self._quarantine_corrupt_file()
            message = (
                f"A(z) '{self.db_file}' sérült, nem tölthető be ({error}). "
                f"Másolat: '{backup}'. Az adatbázis nem íródik felül; helyreállítás: reset vagy a fájl kézi javítása."
            )
            print(f"❌ [REPOSITORY] {message}", flush=True)
            raise DatabaseCorruptedError(message) from error

    def _read_text_with_retry(self) -> str:
        """Windows alatt a fájlcsere pillanatában az olvasás átmenetileg PermissionError-t adhat."""
        last_error = None
        for _ in range(self.READ_RETRIES):
            try:
                with open(self.db_file, "r", encoding="utf-8") as f:
                    return f.read()
            except OSError as error:
                last_error = error
                time.sleep(self.RETRY_DELAY)
        raise DatabaseIOError(f"A(z) '{self.db_file}' nem olvasható: {last_error}") from last_error

    def _quarantine_corrupt_file(self) -> str:
        """Másolatot készít a sérült fájlról (fájlállapotonként egyszer), hogy semmi ne vesszen el."""
        try:
            backup = f"{self.db_file}.corrupt-{os.stat(self.db_file).st_mtime_ns}"
            if not os.path.exists(backup):
                shutil.copy2(self.db_file, backup)
            return backup
        except OSError as error:
            return f"(nem sikerült másolatot készíteni: {error})"

    def _save_db(self, db_data: Dict) -> None:
        """Atomikus mentés: ideiglenes fájlba ír, majd egy lépésben lecseréli az eredetit.

        Hiba esetén kivételt dob (DatabaseIOError), hogy a hívó ne higgye sikeresnek a mentést.
        """
        db_data.setdefault("incidents", {})
        db_data.setdefault("issues", {})
        db_data.setdefault("issue_history", [])
        db_data["total_incidents"] = len(db_data["incidents"])

        cleaned_data = clean_nan(db_data)
        temp_file = f"{self.db_file}.tmp"

        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(cleaned_data, f, indent=4, ensure_ascii=False, default=self._json_default)
                f.flush()
                os.fsync(f.fileno())

            for _ in range(self.REPLACE_RETRIES):
                try:
                    os.replace(temp_file, self.db_file)
                    return
                except PermissionError as error:
                    # Pl. egy olvasó épp nyitva tartja a fájlt; rövid várakozás után újrapróbáljuk
                    last_error = error
                    time.sleep(self.RETRY_DELAY)
            raise last_error
        except Exception as error:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
            message = f"Nem sikerült lementeni a(z) '{self.db_file}' fájlt: {error}"
            print(f"❌ [REPOSITORY SAVE ERROR] {message}", flush=True)
            raise DatabaseIOError(message) from error

    @staticmethod
    def _json_default(value: Any):
        if isinstance(value, Enum):
            return value.value
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return str(value)

    def get_by_id(self, incident_id: str) -> Optional[Incident]:
        with self._write_lock:
            db_data = self._load_db()
        inc_dict = db_data.get("incidents", {}).get(incident_id)
        if not inc_dict:
            return None
        return inc_dict if isinstance(inc_dict, Incident) else Incident.from_dict(inc_dict)

    def get_or_create(self, incident_id: str) -> Incident:
        inc = self.get_by_id(incident_id)
        if inc is None:
            inc = Incident(incident_id=incident_id)
        return inc

    def clear(self) -> None:
        with self._write_lock:
            self._save_db(self._empty_db())

    def count(self) -> int:
        with self._write_lock:
            return len(self._load_db().get("incidents", {}))
