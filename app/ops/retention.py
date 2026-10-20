"""Retention for data that is explicitly eligible for deletion.

Only two kinds of file are ever eligible:
- log files (``*.log``) in ``ops.log_dir`` older than ``ops.log_retention_days``;
- database backups (``paper-*.db`` with their ``.json`` manifests) in
  ``ops.backups_dir`` beyond the newest ``ops.keep_backups``.

Trading, audit, configuration, evaluation and robustness records (the paper
database, reports, baselines) are never touched. The default is a dry run;
deleting needs ``apply=True``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path


def plan_prune(settings, now: datetime) -> list[Path]:
    out: list[Path] = []
    cutoff = (now - timedelta(days=settings.ops.log_retention_days)).timestamp()
    log_dir = Path(settings.ops.log_dir)
    if log_dir.is_dir():
        out += sorted(p for p in log_dir.glob("*.log")
                      if p.is_file() and p.stat().st_mtime < cutoff)
    bdir = Path(settings.ops.backups_dir)
    if bdir.is_dir():
        backups = sorted((p for p in bdir.glob("paper-*.db") if p.is_file()),
                         key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[settings.ops.keep_backups:]:
            out.append(old)
            if old.with_suffix(".json").exists():
                out.append(old.with_suffix(".json"))
    protected = {Path(settings.paper.db_path).resolve(),
                 Path(settings.paper.replay_db_path).resolve()}
    return [p for p in out if p.resolve() not in protected]


def prune(settings, now: datetime, apply: bool = False) -> dict:
    targets = plan_prune(settings, now)
    deleted = []
    if apply:
        for p in targets:
            p.unlink()
            deleted.append(str(p))
    return {"dry_run": not apply, "eligible": [str(p) for p in targets], "deleted": deleted}
