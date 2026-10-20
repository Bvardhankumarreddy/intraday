"""Filesystem safety helpers.

- ``private_dir`` / ``private_file``: owner-only permissions (0700 / 0600)
  for databases, lock files, backups and reports, which hold financial
  records.
- ``safe_child``: join a user-supplied name under a base directory and
  refuse anything that escapes it (``..``, absolute paths, symlinks out).
- ``write_preserving``: write a text file without losing an earlier,
  different version. The previous content is archived next to it under
  ``.history/`` with its hash and modification time. Identical content is a
  no-op, so regenerating a report never multiplies copies.
"""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import UTC, datetime
from pathlib import Path


class UnsafePathError(ValueError):
    pass


def private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(stat.S_IRWXU)
    except PermissionError:  # not ours to change (e.g. a shared mount); reported by ops check
        pass
    return path


def private_file(path: Path) -> Path:
    path = Path(path)
    if path.exists():
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except PermissionError:
            pass
    return path


def safe_child(base: Path, name: str) -> Path:
    base = Path(base).resolve()
    if not name or Path(name).is_absolute():
        raise UnsafePathError(f"refusing path {name!r}: must be relative to {base}")
    target = (base / name).resolve()
    if target != base and base not in target.parents:
        raise UnsafePathError(f"refusing path {name!r}: escapes {base}")
    return target


def write_preserving(path: Path, text: str) -> Path | None:
    """Write ``text`` to ``path``. If a different version exists, archive it
    first and return the archive path (None if nothing was archived)."""
    path = Path(path)
    archived = None
    if path.exists():
        old = path.read_bytes()
        if old == text.encode():
            return None
        digest = hashlib.sha256(old).hexdigest()[:12]
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y%m%dT%H%M%SZ")
        hist = private_dir(path.parent / ".history")
        archived = hist / f"{path.stem}.{mtime}.{digest}{path.suffix}"
        if not archived.exists():
            archived.write_bytes(old)
            private_file(archived)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp.write_text(text)
    os.replace(tmp, path)  # atomic: readers see the old or the new file, never half
    return archived


def free_disk_mb(path: Path) -> float:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1_048_576
