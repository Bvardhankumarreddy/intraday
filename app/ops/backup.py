"""SQLite backup, verification and isolated restore for the paper database.

- ``create_backup`` uses SQLite's online backup API, which takes a
  consistent copy even while the engine is writing in WAL mode. Each backup
  gets a unique timestamped name and is never overwritten. A manifest
  records its SHA-256, size, schema version and source path. Files are 0600.
- ``verify_backup`` checks the manifest hash, then opens a **temporary copy**
  (never the backup itself, never the active database). It runs SQLite's
  integrity check, the schema-version check and the full read-only
  financial reconciliation.
- ``restore_backup`` copies a verified backup to a **new** path. It refuses
  to write over the active database or any existing file. Switching the
  application to the restored copy is a manual, documented step
  (docs/runbooks.md).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

from app.paper.store import PaperStore, StoreError
from app.utils.fs import private_dir, private_file


class BackupError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(db_path: Path, backups_dir: Path, now: datetime) -> dict:
    db_path = Path(db_path)
    if not db_path.is_file():
        raise BackupError(f"no database at {db_path}")
    root = private_dir(Path(backups_dir))
    stem = f"paper-{now.strftime('%Y%m%dT%H%M%S')}"
    target, n = root / f"{stem}.db", 1
    while target.exists() or target.with_suffix(".json").exists():
        n += 1
        target = root / f"{stem}-{n}.db"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        version = dst.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() \
            if dst.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone() else None
    finally:
        dst.close()
        src.close()
    private_file(target)
    if check != "ok":
        raise BackupError(f"backup {target} failed integrity check: {check}")
    manifest = {"backup": target.name, "source": str(db_path), "created_at": now.isoformat(),
                "sha256": _sha(target), "bytes": target.stat().st_size,
                "schema_version": int(version[0]) if version else 1,
                "integrity_check": check}
    man = target.with_suffix(".json")
    man.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    private_file(man)
    return {**manifest, "path": str(target), "manifest": str(man)}


def verify_backup(backup: Path, settings, today: date | None = None,
                  active_db: Path | None = None) -> dict:
    """Verify in an isolated temporary copy. Read-only for everything else."""
    from app.costs.schedule import load_fee_schedule
    from app.ops.reconciliation import full_reconciliation

    backup = Path(backup)
    if active_db is not None and backup.resolve() == Path(active_db).resolve():
        raise BackupError("refusing to verify the active database as a backup")
    result = {"backup": str(backup), "checks": []}

    def check(name, ok, detail=""):
        result["checks"].append({"check": name, "status": "PASS" if ok else "FAIL",
                                 "detail": detail})
        return ok

    man_path = backup.with_suffix(".json")
    if man_path.exists():
        man = json.loads(man_path.read_text())
        check("manifest_sha256", man["sha256"] == _sha(backup),
              "hash matches manifest" if man["sha256"] == _sha(backup) else "HASH MISMATCH")
    else:
        check("manifest_sha256", False, "manifest missing")
    with tempfile.TemporaryDirectory(prefix="paper-verify-") as tmp:
        copy = Path(tmp) / "verify.db"
        shutil.copyfile(backup, copy)
        try:
            store = PaperStore(copy)  # integrity + schema checks happen here
        except StoreError as exc:
            check("open_integrity_schema", False, str(exc))
            result["status"] = "FAIL"
            return result
        try:
            check("open_integrity_schema", True, f"schema version {store.schema_version()}")
            try:
                schedule = load_fee_schedule(settings)
            except Exception:  # noqa: BLE001
                schedule = None
            rec = full_reconciliation(store, settings, schedule, today or date.today())
            check("reconciliation", rec["counts"]["CRITICAL"] == 0 and rec["counts"]["ERROR"] == 0,
                  f"{rec['status']} {rec['counts']}")
            result["portfolio"] = rec["portfolio"]
            result["rows"] = {t: store.one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
                              for t in ("sessions", "signals", "orders", "positions", "events",
                                        "reports")}
        finally:
            store.close()
    result["status"] = "PASS" if all(c["status"] == "PASS" for c in result["checks"]) \
        else "FAIL"
    return result


def restore_backup(backup: Path, target: Path, active_db: Path, settings) -> dict:
    """Copy a verified backup to a NEW path. Never overwrites anything."""
    backup, target = Path(backup), Path(target)
    if target.resolve() == Path(active_db).resolve():
        raise BackupError("refusing to restore over the active database; restore to a new "
                          "path, verify it, then switch over manually (see runbooks)")
    if target.exists():
        raise BackupError(f"{target} already exists; choose a new path")
    verification = verify_backup(backup, settings, active_db=active_db)
    if verification["status"] != "PASS":
        raise BackupError(f"backup failed verification: {verification['checks']}")
    private_dir(target.parent)
    shutil.copyfile(backup, target)
    private_file(target)
    if _sha(target) != _sha(backup):
        raise BackupError("restored copy does not match the backup")
    return {"restored_to": str(target), "verification": verification}
