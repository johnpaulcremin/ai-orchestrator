from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.database import _MIGRATIONS, _run_migrations, create_conversation, init_db


def _user_version(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _index_names(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}


def _backup_files(db_path: Path) -> list[Path]:
    return sorted(db_path.parent.glob(f"{db_path.name}.bak-*"))


def test_latest_migration_number_matches_the_defined_count() -> None:
    """Catches a copy-paste version-number bug (a gap or a duplicate) at
    collection time rather than at runtime on someone's real database."""
    numbers = [version for version, _description, _fn in _MIGRATIONS]
    assert numbers == list(range(1, len(_MIGRATIONS) + 1))


def test_init_db_on_a_fresh_database_applies_every_migration(db_path: Path) -> None:
    assert _user_version(db_path) == len(_MIGRATIONS)
    assert "idx_conversations_owner" in _index_names(db_path, "conversations")
    assert "idx_templates_owner" in _index_names(db_path, "templates")
    # A first-ever init_db() always has pending migrations (user_version
    # starts at 0), so it always backs up -- even for a database that, by
    # the time _run_migrations actually runs, already has its baseline
    # tables from the CREATE TABLE statements earlier in the same call.
    assert len(_backup_files(db_path)) == 1


def test_init_db_called_again_does_not_redo_migrations(db_path: Path) -> None:
    before = _backup_files(db_path)

    init_db()

    assert _user_version(db_path) == len(_MIGRATIONS)
    # No pending migrations the second time around, so no new backup either.
    assert _backup_files(db_path) == before


def test_migrations_run_against_a_database_that_predates_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates a real upgrade: a database created before this mechanism
    existed (baseline schema, but PRAGMA user_version still at SQLite's
    default of 0 and no owner indexes) must pick up every migration the
    next time init_db() runs, and existing data must survive untouched."""
    db_path = tmp_path / "pre_migration.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                owner TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            "CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT)"
        )
        conn.execute(
            "INSERT INTO conversations (title, owner) VALUES ('Predates migrations', 'alice')"
        )
        conn.commit()
    assert _user_version(db_path) == 0

    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    init_db()

    assert _user_version(db_path) == len(_MIGRATIONS)
    assert "idx_conversations_owner" in _index_names(db_path, "conversations")
    assert "idx_templates_owner" in _index_names(db_path, "templates")
    with sqlite3.connect(db_path) as conn:
        title = conn.execute("SELECT title FROM conversations").fetchone()[0]
    assert title == "Predates migrations"

    backups = _backup_files(db_path)
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        # The backup is a snapshot from BEFORE the migration ran: the index
        # must not be there yet, but the pre-existing row must be.
        assert "idx_conversations_owner" not in {
            row[1] for row in conn.execute("PRAGMA index_list(conversations)")
        }
        title = conn.execute("SELECT title FROM conversations").fetchone()[0]
    assert title == "Predates migrations"


def test_run_migrations_is_a_no_op_with_nothing_pending(tmp_path: Path) -> None:
    """Once a database is already at the latest version, calling
    _run_migrations again touches nothing -- no backup, no version churn."""
    db_path = tmp_path / "already_current.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE conversations (id INTEGER PRIMARY KEY, owner TEXT)")
        conn.execute("CREATE TABLE templates (id INTEGER PRIMARY KEY, owner TEXT)")
        conn.execute(f"PRAGMA user_version = {len(_MIGRATIONS)}")
        conn.commit()

        _run_migrations(conn)

    assert _backup_files(db_path) == []
    assert _user_version(db_path) == len(_MIGRATIONS)


def test_create_conversation_still_works_after_migrations(db_path: Path) -> None:
    conversation = create_conversation("After migrations", owner="bob")
    assert conversation["title"] == "After migrations"
