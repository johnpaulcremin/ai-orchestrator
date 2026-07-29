"""Rotating periodic backups of the whole SQLite database file — distinct
from database.py's migration-time backup (a one-off safety copy taken right
before a schema change). This is an ongoing "the working data itself grows
and could get corrupted/accidentally deleted" safeguard: a personal,
local-first deployment has no operator-managed backup infrastructure of its
own, so the app takes its own periodic snapshots.

No background scheduler — same "no cron-like thread" convention as
model_catalog.py's sync_if_stale(): backup_if_due() is a cheap staleness
check (glob the backup directory, compare the newest file's timestamp)
that's safe to call on every hit of a naturally-frequent request path (see
routers/conversations.py's `conversations()` — hit every time the sidebar
loads), and only actually copies the ~megabyte(s) database file + rotates
old backups on the rare call where a backup is actually due.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import _connect, _db_path
from .settings import bool_setting
from .telemetry import logger

_DEFAULT_INTERVAL_HOURS = 24
_DEFAULT_MAX_BACKUPS = 7
# Distinct prefix from database._backup_db_path's ".bak-v{version}-..." so
# rotation here never touches (or counts) a migration-time backup.
_BACKUP_SUFFIX_RE = re.compile(r"\.backup-(\d{8}T\d{6}Z)$")


def enabled() -> bool:
    """On by default (like IMAGE_DOWNSCALE/OCR_REPLACEMENT): a local file
    copy costs nothing and never changes answering behavior, unlike the
    flags that default off (spend money, or change what gets served)."""
    return bool_setting("DB_BACKUP", True)


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def interval_hours() -> int:
    return _int_env("DB_BACKUP_INTERVAL_HOURS", _DEFAULT_INTERVAL_HOURS)


def max_backups() -> int:
    return _int_env("DB_BACKUP_MAX_COUNT", _DEFAULT_MAX_BACKUPS)


def _backup_path(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return db_path.with_name(f"{db_path.name}.backup-{timestamp}")


def _existing_backups(db_path: Path) -> list[Path]:
    """Every periodic backup file for this database, oldest first — sorted
    by the timestamp encoded in the filename (not mtime, which a file copy
    or restore could disturb)."""
    if not db_path.parent.is_dir():
        return []
    candidates = [
        p
        for p in db_path.parent.glob(f"{db_path.name}.backup-*")
        if _BACKUP_SUFFIX_RE.search(p.name)
    ]
    return sorted(candidates, key=lambda p: _BACKUP_SUFFIX_RE.search(p.name).group(1))  # type: ignore[union-attr]


def last_backup_at() -> datetime | None:
    """The newest periodic backup's timestamp (parsed from its filename), or
    None if none exist yet."""
    backups = _existing_backups(_db_path())
    if not backups:
        return None
    match = _BACKUP_SUFFIX_RE.search(backups[-1].name)
    assert match is not None  # guaranteed by _existing_backups' own filter
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
        tzinfo=timezone.utc
    )


def is_due() -> bool:
    last = last_backup_at()
    if last is None:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=interval_hours())


def _rotate(db_path: Path) -> None:
    backups = _existing_backups(db_path)
    cap = max_backups()
    excess = len(backups) - cap
    for old in backups[:excess]:
        old.unlink(missing_ok=True)


def backup_now() -> Path | None:
    """Take a fresh backup unconditionally (ignoring is_due()) and rotate
    old ones, or None if there's no database file worth protecting yet (a
    fresh install, or a test's throwaway path that was never initialized).
    Never raises — a failed backup must not break whatever request path
    triggered it; the failure is logged instead."""
    db_path = _db_path()
    if not db_path.exists() or db_path.stat().st_size == 0:
        return None

    try:
        # Fold the WAL into the main file first, same reasoning as the
        # migration-time backup: otherwise the copy could miss whatever's
        # still only in the -wal sidecar.
        with _connect() as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        backup_path = _backup_path(db_path)
        shutil.copy2(db_path, backup_path)
        _rotate(db_path)
        logger.info("db.periodic_backup path=%s", backup_path)
        return backup_path
    except (OSError, sqlite3.Error):
        logger.warning("db.periodic_backup_failed", exc_info=True)
        return None


def backup_if_due() -> Path | None:
    """The actual entry point request paths call: a no-op unless the
    feature is enabled AND a backup is actually due, so this is cheap to
    call on every hit of a frequent route."""
    if not enabled() or not is_due():
        return None
    return backup_now()
