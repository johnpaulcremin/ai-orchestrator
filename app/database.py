from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any


def _db_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "ai_orchestrator.db"))


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                mode_used TEXT,
                notes TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conversation_id) REFERENCES conversations(id)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
            ON messages(conversation_id)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Runtime-editable settings (the task->model map). Global: one row per
        # settable key. See app/settings.py for the resolution precedence.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Response cache: an identical prompt (same mode + model config) returns
        # the stored answer without any model call. See app/cache.py.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS response_cache (
                key TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                mode TEXT NOT NULL,
                answer TEXT NOT NULL,
                mode_used TEXT,
                notes TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_hit_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        # Migration: add conversations.owner (NULL = shared / created without a
        # logged-in user) if an older DB predates per-user isolation.
        conversation_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(conversations)")
        }
        if "owner" not in conversation_columns:
            conn.execute("ALTER TABLE conversations ADD COLUMN owner TEXT")

        # Migration: add token/cost columns to messages if an older DB predates
        # usage tracking.
        message_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(messages)")
        }
        for column, coltype in (
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("cost_usd", "REAL"),
            # 1 when this assistant message was served from the response cache.
            ("cached", "INTEGER"),
        ):
            if column not in message_columns:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {coltype}")


def get_settings() -> dict[str, str]:
    """All persisted settings as a {key: value} map."""
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str) -> None:
    """Upsert a single setting."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, value),
        )


def delete_setting(key: str) -> bool:
    """Remove a setting. Returns True if a row was deleted."""
    with _connect() as conn:
        cursor = conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    return cursor.rowcount > 0


def clear_settings() -> None:
    """Remove every persisted setting (revert the whole map to env/defaults)."""
    with _connect() as conn:
        conn.execute("DELETE FROM settings")


def cache_get(key: str) -> dict[str, Any] | None:
    """A cache row plus its age in seconds, or None if absent."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT key, question, mode, answer, mode_used, notes, model,
                   input_tokens, output_tokens, cost_usd, hit_count,
                   CAST(strftime('%s', 'now') - strftime('%s', created_at)
                        AS INTEGER) AS age_seconds
            FROM response_cache
            WHERE key = ?
            """,
            (key,),
        ).fetchone()
    return dict(row) if row else None


def cache_put(
    key: str,
    question: str,
    mode: str,
    answer: str,
    mode_used: str | None,
    notes: str | None,
    model: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    """Insert or replace a cache entry (a replace resets its age / TTL clock)."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO response_cache
                (key, question, mode, answer, mode_used, notes, model,
                 input_tokens, output_tokens, cost_usd,
                 created_at, last_hit_at, hit_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0)
            ON CONFLICT(key) DO UPDATE SET
                answer = excluded.answer,
                mode_used = excluded.mode_used,
                notes = excluded.notes,
                model = excluded.model,
                input_tokens = excluded.input_tokens,
                output_tokens = excluded.output_tokens,
                cost_usd = excluded.cost_usd,
                created_at = CURRENT_TIMESTAMP
            """,
            (
                key,
                question,
                mode,
                answer,
                mode_used,
                notes,
                model,
                input_tokens,
                output_tokens,
                cost_usd,
            ),
        )


def cache_touch(key: str) -> None:
    """Record a cache hit (updates last_hit_at + hit_count)."""
    with _connect() as conn:
        conn.execute(
            """
            UPDATE response_cache
            SET last_hit_at = CURRENT_TIMESTAMP, hit_count = hit_count + 1
            WHERE key = ?
            """,
            (key,),
        )


def cache_delete(key: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM response_cache WHERE key = ?", (key,))


def cache_count() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM response_cache").fetchone()
    return int(row["n"]) if row else 0


def cache_delete_oldest(count: int) -> None:
    """Evict the `count` least-recently-hit entries."""
    if count <= 0:
        return
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM response_cache
            WHERE key IN (
                SELECT key FROM response_cache
                ORDER BY last_hit_at ASC, created_at ASC
                LIMIT ?
            )
            """,
            (count,),
        )


def cache_clear() -> int:
    """Remove every cache entry. Returns the number removed."""
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM response_cache").fetchone()["n"]
        conn.execute("DELETE FROM response_cache")
    return int(count)


def create_user(username: str, password_hash: str) -> dict[str, Any] | None:
    """Insert a user. Returns the new row, or None if the username is taken."""
    try:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, password_hash),
            )
            user_id = cursor.lastrowid

            row = conn.execute(
                "SELECT id, username, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
    except sqlite3.IntegrityError:
        return None

    return dict(row)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, username, password_hash, created_at
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

    return dict(row) if row else None


def create_conversation(title: str, owner: str | None = None) -> dict[str, Any]:
    clean_title = title.strip() or "Untitled conversation"

    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO conversations (title, owner) VALUES (?, ?)",
            (clean_title, owner),
        )
        conversation_id = cursor.lastrowid

        row = conn.execute(
            """
            SELECT id, title, owner, created_at, updated_at
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()

    return dict(row)


def list_conversations(owner: str | None = None) -> list[dict[str, Any]]:
    # owner is None for the shared/unauthenticated bucket (owner IS NULL);
    # a username returns only that user's conversations.
    with _connect() as conn:
        if owner is None:
            rows = conn.execute(
                """
                SELECT id, title, owner, created_at, updated_at
                FROM conversations
                WHERE owner IS NULL
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, title, owner, created_at, updated_at
                FROM conversations
                WHERE owner = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (owner,),
            ).fetchall()

    return [dict(row) for row in rows]


def get_conversation(conversation_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, title, owner, created_at, updated_at
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def update_conversation_title(
    conversation_id: int, title: str
) -> dict[str, Any] | None:
    clean_title = title.strip() or "Untitled conversation"

    with _connect() as conn:
        conn.execute(
            """
            UPDATE conversations
            SET title = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (clean_title, conversation_id),
        )

        row = conn.execute(
            """
            SELECT id, title, owner, created_at, updated_at
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()

    return dict(row) if row else None


def delete_conversation(conversation_id: int) -> bool:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()

        if not existing:
            return False

        conn.execute(
            "DELETE FROM messages WHERE conversation_id = ?",
            (conversation_id,),
        )

        conn.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )

    return True


_MESSAGE_COLUMNS = (
    "id, conversation_id, role, content, mode_used, notes, "
    "input_tokens, output_tokens, cost_usd, cached, created_at"
)


def add_message(
    conversation_id: int,
    role: str,
    content: str,
    mode_used: str | None = None,
    notes: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    cached: bool = False,
) -> dict[str, Any]:
    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages
                (conversation_id, role, content, mode_used, notes,
                 input_tokens, output_tokens, cost_usd, cached)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conversation_id,
                role,
                content,
                mode_used,
                notes,
                input_tokens,
                output_tokens,
                cost_usd,
                1 if cached else 0,
            ),
        )

        conn.execute(
            """
            UPDATE conversations
            SET updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (conversation_id,),
        )

        message_id = cursor.lastrowid

        row = conn.execute(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()

    return dict(row)


def list_messages(conversation_id: int) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()

    return [dict(row) for row in rows]
