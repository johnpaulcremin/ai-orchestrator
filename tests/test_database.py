from __future__ import annotations

import sqlite3
from pathlib import Path

from app.database import (
    add_message,
    clear_summary_cache,
    create_conversation,
    delete_conversation,
    delete_messages_after,
    delete_messages_from,
    get_conversation,
    get_summary_cache,
    init_db,
    list_conversations,
    list_messages,
    set_summary_cache,
    update_conversation_title,
)


def _backdate_conversations(db_path: Path) -> None:
    """Push every conversation's updated_at far into the past."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE conversations SET updated_at = '2000-01-01 00:00:00'")


def test_init_db_is_idempotent(db_path: Path) -> None:
    init_db()
    init_db()

    conversation = create_conversation("Survives re-init")
    assert conversation["title"] == "Survives re-init"


def test_create_get_and_list_conversations(db_path: Path) -> None:
    assert list_conversations() == []

    created = create_conversation("First conversation")

    assert created["id"] == 1
    assert created["title"] == "First conversation"
    assert created["created_at"]
    assert created["updated_at"]

    fetched = get_conversation(created["id"])
    assert fetched == created

    assert get_conversation(999) is None

    listed = list_conversations()
    assert [row["id"] for row in listed] == [created["id"]]


def test_create_conversation_blank_title_becomes_untitled(db_path: Path) -> None:
    conversation = create_conversation("   ")
    assert conversation["title"] == "Untitled conversation"


def test_update_conversation_title(db_path: Path) -> None:
    conversation = create_conversation("Old title")

    updated = update_conversation_title(conversation["id"], "New title")

    assert updated is not None
    assert updated["id"] == conversation["id"]
    assert updated["title"] == "New title"

    assert update_conversation_title(999, "Nope") is None


def test_delete_conversation_removes_messages(db_path: Path) -> None:
    conversation = create_conversation("Doomed")
    add_message(conversation["id"], role="user", content="hello")
    add_message(conversation["id"], role="assistant", content="hi")

    assert delete_conversation(conversation["id"]) is True

    assert get_conversation(conversation["id"]) is None
    assert list_messages(conversation["id"]) == []

    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert remaining == 0

    assert delete_conversation(conversation["id"]) is False
    assert delete_conversation(999) is False


def test_add_message_returns_full_row(db_path: Path) -> None:
    conversation = create_conversation("Chat")

    message = add_message(
        conversation["id"],
        role="assistant",
        content="the answer",
        mode_used="auto->fast",
        notes="some notes",
    )

    assert message["id"] == 1
    assert message["conversation_id"] == conversation["id"]
    assert message["role"] == "assistant"
    assert message["content"] == "the answer"
    assert message["mode_used"] == "auto->fast"
    assert message["notes"] == "some notes"
    assert message["created_at"]


def test_add_message_bumps_conversation_ordering(db_path: Path) -> None:
    conv_a = create_conversation("Conversation A")
    conv_b = create_conversation("Conversation B")

    _backdate_conversations(db_path)

    # With identical (old) timestamps the id tiebreak puts B first.
    assert [row["id"] for row in list_conversations()] == [
        conv_b["id"],
        conv_a["id"],
    ]

    add_message(conv_a["id"], role="user", content="bump")

    # The new message refreshed A's updated_at, so A now sorts first.
    assert [row["id"] for row in list_conversations()] == [
        conv_a["id"],
        conv_b["id"],
    ]


def test_list_messages_ordered_by_id(db_path: Path) -> None:
    conversation = create_conversation("Ordered")

    add_message(conversation["id"], role="user", content="first")
    add_message(conversation["id"], role="assistant", content="second")
    add_message(conversation["id"], role="user", content="third")

    messages = list_messages(conversation["id"])

    assert [m["content"] for m in messages] == ["first", "second", "third"]
    assert [m["id"] for m in messages] == sorted(m["id"] for m in messages)

    assert list_messages(999) == []


# --- summary_cache: cached history-summary round trip + invalidation ---------


def test_summary_cache_missing_is_none(db_path: Path) -> None:
    assert get_summary_cache(999) is None


def test_summary_cache_set_then_get_round_trips(db_path: Path) -> None:
    conversation = create_conversation("t")
    set_summary_cache(conversation["id"], 8, "the summary")
    cached = get_summary_cache(conversation["id"])
    assert cached is not None
    assert cached["older_count"] == 8
    assert cached["summary"] == "the summary"


def test_summary_cache_set_upserts(db_path: Path) -> None:
    conversation = create_conversation("t")
    set_summary_cache(conversation["id"], 8, "first")
    set_summary_cache(conversation["id"], 10, "second")
    cached = get_summary_cache(conversation["id"])
    assert cached is not None
    assert cached["older_count"] == 10
    assert cached["summary"] == "second"


def test_summary_cache_clear(db_path: Path) -> None:
    conversation = create_conversation("t")
    set_summary_cache(conversation["id"], 8, "the summary")
    clear_summary_cache(conversation["id"])
    assert get_summary_cache(conversation["id"]) is None


def test_delete_messages_after_invalidates_summary_cache(db_path: Path) -> None:
    conversation = create_conversation("t")
    add_message(conversation["id"], role="user", content="a")
    kept = add_message(conversation["id"], role="assistant", content="b")
    add_message(conversation["id"], role="user", content="c")
    set_summary_cache(conversation["id"], 5, "stale")

    delete_messages_after(conversation["id"], int(kept["id"]))

    assert get_summary_cache(conversation["id"]) is None


def test_delete_messages_from_invalidates_summary_cache(db_path: Path) -> None:
    conversation = create_conversation("t")
    add_message(conversation["id"], role="user", content="a")
    target = add_message(conversation["id"], role="assistant", content="b")
    set_summary_cache(conversation["id"], 5, "stale")

    delete_messages_from(conversation["id"], int(target["id"]))

    assert get_summary_cache(conversation["id"]) is None


def test_delete_conversation_removes_its_summary_cache(db_path: Path) -> None:
    conversation = create_conversation("t")
    set_summary_cache(conversation["id"], 5, "stale")

    delete_conversation(conversation["id"])

    assert get_summary_cache(conversation["id"]) is None
