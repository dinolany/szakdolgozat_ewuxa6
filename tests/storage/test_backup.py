"""Tesztek: storage/backup.py."""
from pathlib import Path
import tempfile
import unittest

from storage import backup


class BackupTest(unittest.TestCase):
    def test_snapshot_throttles_restores_and_keeps_daily(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            a = base / "a.json"
            a.write_text("1", encoding="utf-8")
            first = backup.snapshot([a], base / "backups")
            self.assertIsNotNone(first)
            self.assertIsNone(backup.snapshot([a], base / "backups"))               # túl friss: nincs új
            a.write_text("2", encoding="utf-8")
            backup.restore(base / "backups", first, [a])
            self.assertEqual(a.read_text(encoding="utf-8"), "1")
            names = [s["name"] for s in backup.list_snapshots(base / "backups")]
            self.assertTrue(any(n.startswith("napi-") for n in names))
            with self.assertRaises(ValueError):
                backup.restore(base / "backups", "../a.json", [a])                 # csak a saját mentései közül


if __name__ == "__main__":
    unittest.main()
