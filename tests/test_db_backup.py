"""Rotating periodic DB backups (app/db_backup.py): staleness gating,
rotation, and the on-request-path trigger wired into GET /v1/conversations.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db_backup


def _write_backup(db_path: Path, when: datetime) -> Path:
    """Directly create a periodic-backup file timestamped at `when`, bypassing
    backup_now()'s real wall-clock timestamp so rotation/staleness tests are
    deterministic instead of racing real elapsed time."""
    stamp = when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = db_path.with_name(f"{db_path.name}.backup-{stamp}")
    path.write_bytes(b"fake db snapshot")
    return path


# --- config parsing ----------------------------------------------------------------


def test_enabled_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_BACKUP", raising=False)
    assert db_backup.enabled() is True


def test_enabled_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_BACKUP", "false")
    assert db_backup.enabled() is False


def test_interval_hours_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_BACKUP_INTERVAL_HOURS", raising=False)
    assert db_backup.interval_hours() == 24
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "6")
    assert db_backup.interval_hours() == 6


def test_interval_hours_invalid_or_non_positive_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "not-a-number")
    assert db_backup.interval_hours() == 24
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "0")
    assert db_backup.interval_hours() == 24
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "-5")
    assert db_backup.interval_hours() == 24


def test_max_backups_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_BACKUP_MAX_COUNT", raising=False)
    assert db_backup.max_backups() == 7
    monkeypatch.setenv("DB_BACKUP_MAX_COUNT", "3")
    assert db_backup.max_backups() == 3


# --- last_backup_at() / is_due() ----------------------------------------------------


def test_last_backup_at_is_none_with_no_backups(db_path: Path) -> None:
    assert db_backup.last_backup_at() is None


def test_last_backup_at_returns_the_newest_timestamp(db_path: Path) -> None:
    older = datetime.now(timezone.utc) - timedelta(days=2)
    newer = datetime.now(timezone.utc) - timedelta(hours=1)
    _write_backup(db_path, older)
    _write_backup(db_path, newer)
    last = db_backup.last_backup_at()
    assert last is not None
    assert abs((last - newer).total_seconds()) < 1


def test_is_due_when_no_backups_exist(db_path: Path) -> None:
    assert db_backup.is_due() is True


def test_is_due_false_for_a_recent_backup(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "24")
    _write_backup(db_path, datetime.now(timezone.utc) - timedelta(hours=1))
    assert db_backup.is_due() is False


def test_is_due_true_once_the_interval_has_elapsed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "24")
    _write_backup(db_path, datetime.now(timezone.utc) - timedelta(hours=25))
    assert db_backup.is_due() is True


def test_migration_backups_are_never_counted_as_periodic_backups(
    db_path: Path,
) -> None:
    # database._backup_db_path's own naming convention (".bak-v{n}-...") --
    # must not be mistaken for a periodic backup by is_due()/last_backup_at().
    migration_backup = db_path.with_name(f"{db_path.name}.bak-v0-20260101T000000Z")
    migration_backup.write_bytes(b"migration snapshot")
    assert db_backup.last_backup_at() is None
    assert db_backup.is_due() is True


# --- backup_now() -------------------------------------------------------------------


def test_backup_now_returns_none_when_there_is_no_database_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "does-not-exist.db"))
    assert db_backup.backup_now() is None


def test_backup_now_creates_a_real_backup_file(db_path: Path) -> None:
    backup = db_backup.backup_now()
    assert backup is not None
    assert backup.exists()
    assert backup.name.startswith(f"{db_path.name}.backup-")
    assert backup.read_bytes()  # non-empty: a real copy of the db file


def test_backup_now_updates_last_backup_at(db_path: Path) -> None:
    assert db_backup.last_backup_at() is None
    db_backup.backup_now()
    last = db_backup.last_backup_at()
    assert last is not None
    assert abs((datetime.now(timezone.utc) - last).total_seconds()) < 5


def test_backup_now_rotates_old_backups_beyond_the_cap(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP_MAX_COUNT", "2")
    now = datetime.now(timezone.utc)
    _write_backup(db_path, now - timedelta(days=3))
    _write_backup(db_path, now - timedelta(days=2))
    _write_backup(db_path, now - timedelta(days=1))
    # backup_now() adds a 4th, then rotation must bring the total back to 2
    # (the cap) -- keeping the newest ones.
    db_backup.backup_now()
    remaining = db_backup._existing_backups(db_path)
    assert len(remaining) == 2


def test_backup_now_tolerates_a_copy_failure(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(db_backup.shutil, "copy2", boom)
    assert db_backup.backup_now() is None


# --- backup_if_due() -----------------------------------------------------------------


def test_backup_if_due_is_a_noop_when_disabled(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP", "false")
    assert db_backup.backup_if_due() is None
    assert db_backup.last_backup_at() is None


def test_backup_if_due_is_a_noop_when_not_due(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP_INTERVAL_HOURS", "24")
    _write_backup(db_path, datetime.now(timezone.utc) - timedelta(hours=1))
    before = db_backup._existing_backups(db_path)
    assert db_backup.backup_if_due() is None
    assert db_backup._existing_backups(db_path) == before


def test_backup_if_due_backs_up_when_enabled_and_due(db_path: Path) -> None:
    result = db_backup.backup_if_due()
    assert result is not None
    assert result.exists()


# --- HTTP integration: GET /v1/conversations triggers the check --------------------


def test_get_conversations_triggers_a_due_backup(client: TestClient) -> None:
    assert db_backup.last_backup_at() is None
    r = client.get("/v1/conversations")
    assert r.status_code == 200
    assert db_backup.last_backup_at() is not None


def test_get_conversations_does_not_backup_again_when_not_due(
    client: TestClient,
) -> None:
    client.get("/v1/conversations")
    first = db_backup.last_backup_at()
    client.get("/v1/conversations")
    second = db_backup.last_backup_at()
    assert first == second


def test_get_conversations_never_backs_up_when_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DB_BACKUP", "false")
    client.get("/v1/conversations")
    assert db_backup.last_backup_at() is None


# --- Settings integration ------------------------------------------------------------


def test_db_backup_appears_in_settings_features(client: TestClient) -> None:
    features = client.get("/v1/settings").json()["features"]
    flag = next(f for f in features if f["key"] == "DB_BACKUP")
    assert flag["effective_enabled"] is True  # on by default
    assert flag["default"] is True
