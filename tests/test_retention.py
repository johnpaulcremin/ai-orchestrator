"""Data retention: rollup-before-prune (app/retention.py, app/database.py's
spend_rollup/avoided_cost_rollup/feedback_rollup) and the maintenance pass
that applies it.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, retention

_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _backdate(db_path: Path, table: str, row_id: int, created_at: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE {table} SET created_at = ? WHERE id = ?", (created_at, row_id)
        )


def _insert_spend(db_path: Path, owner: str | None, model: str, cost: float) -> int:
    database.record_spend(owner, model, 100, 50, cost)
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT MAX(id) FROM spend_log").fetchone()[0])


def _insert_avoided_cost(
    db_path: Path, owner: str | None, model: str, cost: float
) -> int:
    database.record_avoided_cost(owner, model, "response_cache_hit", cost)
    with sqlite3.connect(db_path) as conn:
        return int(conn.execute("SELECT MAX(id) FROM avoided_cost_log").fetchone()[0])


def _insert_feedback(db_path: Path, owner: str | None, model: str, verdict: int) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO feedback_log (owner, message_id, model, mode_used, "
            "category, verdict, reason) VALUES (?, 1, ?, 'auto->fast', NULL, ?, NULL)",
            (owner, model, verdict),
        )
        return int(cur.lastrowid)


def _insert_correction(
    db_path: Path,
    owner: str | None,
    model: str,
    mode_used: str = "auto->fast",
    category: str | None = None,
) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO correction_log (owner, message_id, model, mode_used, "
            "category) VALUES (?, 1, ?, ?, ?)",
            (owner, model, mode_used, category),
        )
        return int(cur.lastrowid)


def _insert_fallback(
    db_path: Path, owner: str | None, model: str, reason: str, succeeded: bool = True
) -> int:
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO fallback_log (owner, model, reason, succeeded) "
            "VALUES (?, ?, ?, ?)",
            (owner, model, reason, 1 if succeeded else 0),
        )
        return int(cur.lastrowid)


# --- schema: additive rollup tables (not the numbered _MIGRATIONS system) ---


def _column_names(db_path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_fresh_db_has_the_correction_rollup_table(db_path: Path) -> None:
    assert _column_names(db_path, "correction_rollup") == {
        "id",
        "owner",
        "model",
        "month",
        "flagged_count",
    }


def test_fresh_db_has_the_fallback_rollup_table(db_path: Path) -> None:
    assert _column_names(db_path, "fallback_rollup") == {
        "id",
        "owner",
        "reason",
        "month",
        "count",
    }


# --- Rollup math equals detail -----------------------------------------------


def test_rollup_and_prune_spend_math_matches_detail(db_path: Path) -> None:
    old = _NOW - timedelta(days=400)
    id1 = _insert_spend(db_path, "alice", "gpt-5", 1.5)
    id2 = _insert_spend(db_path, "alice", "gpt-5", 2.5)
    _backdate(db_path, "spend_log", id1, _iso(old))
    _backdate(db_path, "spend_log", id2, _iso(old))

    cutoff = _iso(_NOW - timedelta(days=365))
    pruned = database.rollup_and_prune_spend(cutoff)

    assert pruned == 2
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()[0]
        rollup = conn.execute(
            "SELECT calls, input_tokens, output_tokens, cost_usd FROM spend_rollup "
            "WHERE owner = 'alice' AND model = 'gpt-5'"
        ).fetchone()
    assert remaining == 0
    assert rollup == (2, 200, 100, 4.0)


def test_rollup_accumulates_additively_across_multiple_runs(db_path: Path) -> None:
    # Both land in the same calendar month (June 2025) so they roll into the
    # SAME (owner, model, month) rollup bucket across two separate runs.
    older = _NOW - timedelta(days=375)
    newer = _NOW - timedelta(days=370)
    id1 = _insert_spend(db_path, None, "gpt-5-mini", 1.0)
    id2 = _insert_spend(db_path, None, "gpt-5-mini", 1.0)
    _backdate(db_path, "spend_log", id1, _iso(older))
    _backdate(db_path, "spend_log", id2, _iso(newer))

    # First run only catches the older row (a tighter cutoff than the second).
    first_pruned = database.rollup_and_prune_spend(_iso(_NOW - timedelta(days=372)))
    second_pruned = database.rollup_and_prune_spend(_iso(_NOW - timedelta(days=365)))

    assert first_pruned == 1
    assert second_pruned == 1
    with sqlite3.connect(db_path) as conn:
        rollup = conn.execute(
            "SELECT calls, cost_usd FROM spend_rollup WHERE owner = '' "
            "AND model = 'gpt-5-mini'"
        ).fetchone()
    assert rollup == (2, 2.0)


def test_rollup_and_prune_avoided_cost_math_matches_detail(db_path: Path) -> None:
    old = _NOW - timedelta(days=400)
    row_id = _insert_avoided_cost(db_path, "bob", "gpt-5", 0.4)
    _backdate(db_path, "avoided_cost_log", row_id, _iso(old))

    pruned = database.rollup_and_prune_avoided_cost(_iso(_NOW - timedelta(days=365)))

    assert pruned == 1
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM avoided_cost_log").fetchone()[0]
        rollup = conn.execute(
            "SELECT calls, avoided_cost_usd FROM avoided_cost_rollup "
            "WHERE owner = 'bob' AND model = 'gpt-5'"
        ).fetchone()
    assert remaining == 0
    assert rollup == (1, 0.4)


def test_rollup_and_prune_feedback_math_matches_detail_and_excludes_clears(
    db_path: Path,
) -> None:
    old = _NOW - timedelta(days=400)
    up_id = _insert_feedback(db_path, "alice", "gpt-5", 1)
    down_id = _insert_feedback(db_path, "alice", "gpt-5", -1)
    clear_id = _insert_feedback(db_path, "alice", "gpt-5", 0)
    for row_id in (up_id, down_id, clear_id):
        _backdate(db_path, "feedback_log", row_id, _iso(old))

    pruned = database.rollup_and_prune_feedback(_iso(_NOW - timedelta(days=365)))

    # All 3 rows (including the clear) are deleted from detail...
    assert pruned == 3
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM feedback_log").fetchone()[0]
        rollup = conn.execute(
            "SELECT up_count, down_count FROM feedback_rollup "
            "WHERE owner = 'alice' AND model = 'gpt-5'"
        ).fetchone()
    assert remaining == 0
    # ...but the clear never contributes to the rollup's verdict counts.
    assert rollup == (1, 1)


def test_rollup_and_prune_correction_math_matches_detail(db_path: Path) -> None:
    old = _NOW - timedelta(days=400)
    id1 = _insert_correction(db_path, "alice", "gpt-5")
    id2 = _insert_correction(db_path, "alice", "gpt-5")
    _backdate(db_path, "correction_log", id1, _iso(old))
    _backdate(db_path, "correction_log", id2, _iso(old))

    pruned = database.rollup_and_prune_correction(_iso(_NOW - timedelta(days=365)))

    assert pruned == 2
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM correction_log").fetchone()[0]
        rollup = conn.execute(
            "SELECT flagged_count FROM correction_rollup "
            "WHERE owner = 'alice' AND model = 'gpt-5'"
        ).fetchone()
    assert remaining == 0
    assert rollup == (2,)


def test_correction_rollup_accumulates_additively_across_multiple_runs(
    db_path: Path,
) -> None:
    older = _NOW - timedelta(days=375)
    newer = _NOW - timedelta(days=370)
    id1 = _insert_correction(db_path, None, "gpt-5-mini")
    id2 = _insert_correction(db_path, None, "gpt-5-mini")
    _backdate(db_path, "correction_log", id1, _iso(older))
    _backdate(db_path, "correction_log", id2, _iso(newer))

    first_pruned = database.rollup_and_prune_correction(
        _iso(_NOW - timedelta(days=372))
    )
    second_pruned = database.rollup_and_prune_correction(
        _iso(_NOW - timedelta(days=365))
    )

    assert first_pruned == 1
    assert second_pruned == 1
    with sqlite3.connect(db_path) as conn:
        rollup = conn.execute(
            "SELECT flagged_count FROM correction_rollup WHERE owner = '' "
            "AND model = 'gpt-5-mini'"
        ).fetchone()
    assert rollup == (2,)


def test_rollup_and_prune_fallback_math_matches_detail(db_path: Path) -> None:
    old = _NOW - timedelta(days=400)
    id1 = _insert_fallback(db_path, "alice", "gpt-5", "timeout")
    id2 = _insert_fallback(db_path, "alice", "gpt-5", "timeout", succeeded=False)
    id3 = _insert_fallback(db_path, "alice", "gpt-5", "budget_refusal", succeeded=False)
    for row_id in (id1, id2, id3):
        _backdate(db_path, "fallback_log", row_id, _iso(old))

    pruned = database.rollup_and_prune_fallback(_iso(_NOW - timedelta(days=365)))

    assert pruned == 3
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM fallback_log").fetchone()[0]
        rollup = dict(
            conn.execute(
                "SELECT reason, count FROM fallback_rollup WHERE owner = 'alice'"
            ).fetchall()
        )
    assert remaining == 0
    assert rollup == {"timeout": 2, "budget_refusal": 1}


def test_prune_free_tier_usage_deletes_only_stale_dates(db_path: Path) -> None:
    database.free_tier_usage_set("groq/llama-3.3-70b-versatile", "2020-01-01", 5)
    database.free_tier_usage_set("groq/llama-3.3-70b-versatile", "2026-06-14", 3)

    pruned = database.prune_free_tier_usage("2026-03-17")

    assert pruned == 1
    assert (
        database.free_tier_usage_count("groq/llama-3.3-70b-versatile", "2020-01-01")
        == 0
    )
    assert (
        database.free_tier_usage_count("groq/llama-3.3-70b-versatile", "2026-06-14")
        == 3
    )


# --- Defaults preserve current behaviour byte-for-byte -----------------------


def test_default_retention_never_prunes_recent_rows(db_path: Path) -> None:
    """RETENTION_DAYS_DETAIL unset -> the 365-day default; a row from a few
    days ago must never be touched."""
    recent = _NOW - timedelta(days=5)
    row_id = _insert_spend(db_path, "alice", "gpt-5", 1.0)
    _backdate(db_path, "spend_log", row_id, _iso(recent))

    counts = retention.rollup_and_prune(now=_NOW)

    assert counts["spend_log"] == 0
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()[0]
    assert remaining == 1


def test_retention_days_detail_zero_keeps_everything_forever(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "0")
    ancient = _NOW - timedelta(days=5000)
    row_id = _insert_spend(db_path, "alice", "gpt-5", 1.0)
    _backdate(db_path, "spend_log", row_id, _iso(ancient))

    counts = retention.rollup_and_prune(now=_NOW)

    assert counts["spend_log"] == 0
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()[0]
    assert remaining == 1


def test_retention_days_detail_setting_resolution(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert retention.retention_days_detail() == 365
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    assert retention.retention_days_detail() == 30
    database.set_setting("RETENTION_DAYS_DETAIL", "10")
    assert retention.retention_days_detail() == 10  # override wins over env


def test_share_expiry_days_setting_resolution(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert retention.share_expiry_days() is None
    monkeypatch.setenv("SHARE_EXPIRY_DAYS", "7")
    assert retention.share_expiry_days() == 7


# --- Usage/feedback continuity across the prune boundary ---------------------


def test_usage_by_model_continuity_across_prune_boundary(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window spanning the retention boundary must still report the
    model's full spend — the detail ∪ rollup union app/retention.py adds."""
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    old_id = _insert_spend(db_path, None, "gpt-5", 3.0)
    _backdate(db_path, "spend_log", old_id, _iso(_NOW - timedelta(days=60)))
    _insert_spend(db_path, None, "gpt-5", 2.0)  # stays recent, untouched

    retention.rollup_and_prune(now=_NOW)
    # Confirm the older row really was pruned out of detail.
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM spend_log").fetchone()[0]
    assert remaining == 1

    start_month = retention.window_start_month(90, now=_NOW)
    by_model = retention.fold_rollup_into_by_model(
        [
            {
                "model": "gpt-5",
                "calls": 1,
                "input_tokens": 100,
                "output_tokens": 50,
                "cost_usd": 2.0,
            }
        ],
        None,
        start_month,
    )
    assert by_model == [
        {
            "model": "gpt-5",
            "calls": 2,
            "input_tokens": 200,
            "output_tokens": 100,
            "cost_usd": 5.0,
        }
    ]


def test_usage_by_model_rollup_outside_window_is_excluded(db_path: Path) -> None:
    """A rollup row from a month well before the requested window must NOT
    be folded in — window_start_month bounds which months are eligible."""
    old_id = _insert_spend(db_path, None, "gpt-5", 9.0)
    _backdate(db_path, "spend_log", old_id, _iso(_NOW - timedelta(days=500)))
    database.rollup_and_prune_spend(_iso(_NOW - timedelta(days=365)))

    start_month = retention.window_start_month(14, now=_NOW)  # a recent, narrow window
    by_model = retention.fold_rollup_into_by_model([], None, start_month)

    assert by_model == []


def test_feedback_by_model_continuity_across_prune_boundary(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    up_id = _insert_feedback(db_path, "alice", "gpt-5", 1)
    down_id = _insert_feedback(db_path, "alice", "gpt-5", -1)
    for row_id in (up_id, down_id):
        _backdate(db_path, "feedback_log", row_id, _iso(_NOW - timedelta(days=60)))

    retention.rollup_and_prune(now=_NOW)

    start_month = retention.window_start_month(90, now=_NOW)
    merged = retention.fold_rollup_into_feedback_by_model({}, "alice", start_month)

    assert merged == {
        "gpt-5": {"answers_rated": 2, "up": 1, "down": 1, "down_rate": 0.5}
    }


def test_correction_by_model_continuity_across_prune_boundary(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window spanning the retention boundary must still count a
    correction flag that's since been rolled up and pruned out of
    correction_log — same reconciliation as feedback's own equivalent."""
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    id1 = _insert_correction(db_path, "alice", "gpt-5")
    _backdate(db_path, "correction_log", id1, _iso(_NOW - timedelta(days=60)))
    _insert_correction(db_path, "alice", "gpt-5")  # stays recent, untouched

    counts = retention.rollup_and_prune(now=_NOW)
    assert counts["correction_log"] == 1
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM correction_log").fetchone()[0]
    assert remaining == 1

    start_month = retention.window_start_month(90, now=_NOW)
    by_model = retention.fold_rollup_into_correction_by_model(
        {"gpt-5": {"flagged": 1, "answers": 5, "correction_rate": 0.2}},
        "alice",
        start_month,
    )
    assert by_model == {
        "gpt-5": {"flagged": 2, "answers": 5, "correction_rate": pytest.approx(0.4)}
    }

    overall = retention.fold_rollup_into_correction_overall(
        {"flagged": 1, "answers": 5, "correction_rate": 0.2}, "alice", start_month
    )
    assert overall == {
        "flagged": 2,
        "answers": 5,
        "correction_rate": pytest.approx(0.4),
    }


def test_fallback_reasons_continuity_across_prune_boundary(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    id1 = _insert_fallback(db_path, "alice", "gpt-5", "timeout")
    _backdate(db_path, "fallback_log", id1, _iso(_NOW - timedelta(days=60)))
    _insert_fallback(db_path, "alice", "gpt-5", "timeout")  # stays recent

    counts = retention.rollup_and_prune(now=_NOW)
    assert counts["fallback_log"] == 1
    with sqlite3.connect(db_path) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM fallback_log").fetchone()[0]
    assert remaining == 1

    start_month = retention.window_start_month(90, now=_NOW)
    merged = retention.fold_rollup_into_fallback_reasons(
        [{"reason": "timeout", "count": 1}], "alice", start_month
    )
    assert merged == [{"reason": "timeout", "count": 2}]


# --- Share expiry honoured ----------------------------------------------------


def test_create_share_applies_default_expiry_when_no_ttl_given(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHARE_EXPIRY_DAYS", "3")
    cid = client.post("/v1/conversations", json={"title": "t"}).json()["id"]

    res = client.post(f"/v1/conversations/{cid}/share", json={})

    expires_at = res.json()["expires_at"]
    assert expires_at is not None
    expires_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    delta = expires_dt - datetime.now(timezone.utc)
    assert timedelta(days=2, hours=23) < delta < timedelta(days=3, hours=1)


def test_explicit_ttl_hours_overrides_the_default_expiry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHARE_EXPIRY_DAYS", "3")
    cid = client.post("/v1/conversations", json={"title": "t"}).json()["id"]

    res = client.post(f"/v1/conversations/{cid}/share", json={"ttl_hours": 1})

    expires_at = res.json()["expires_at"]
    expires_dt = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    delta = expires_dt - datetime.now(timezone.utc)
    assert timedelta(minutes=55) < delta < timedelta(minutes=65)


def test_no_default_expiry_leaves_share_link_live_until_revoked(
    client: TestClient,
) -> None:
    """SHARE_EXPIRY_DAYS unset -> unchanged pre-existing behavior: no
    ttl_hours given means the link never expires."""
    cid = client.post("/v1/conversations", json={"title": "t"}).json()["id"]

    res = client.post(f"/v1/conversations/{cid}/share", json={})

    assert res.json()["expires_at"] is None


# --- Maintenance scheduling ---------------------------------------------------


def test_maintenance_is_due_on_first_run_and_not_immediately_after(
    db_path: Path,
) -> None:
    assert retention.is_due(now=_NOW) is True
    # record_maintenance_run() stamps the REAL current time (CURRENT_TIMESTAMP),
    # so drive is_due() off real "now" here rather than the fixed _NOW fixture,
    # to keep the interval math meaningful regardless of when the test runs.
    database.record_maintenance_run()
    real_now = datetime.now(timezone.utc)
    assert retention.is_due(now=real_now) is False
    assert retention.is_due(now=real_now + timedelta(days=8)) is True


def test_maintenance_if_due_noop_when_backup_did_not_run(db_path: Path) -> None:
    assert retention.maintenance_if_due(backup_just_ran=False) is None
    assert database.last_maintenance_run_at() is None


def test_maintenance_if_due_runs_rollup_and_records_when_due(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RETENTION_DAYS_DETAIL", "30")
    old_id = _insert_spend(db_path, None, "gpt-5", 1.0)
    _backdate(db_path, "spend_log", old_id, "2000-01-01 00:00:00")

    result = retention.maintenance_if_due(backup_just_ran=True)

    assert result is not None
    assert result["spend_log"] == 1
    assert database.last_maintenance_run_at() is not None


def test_maintenance_never_triggers_on_ask_paths(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ask/regenerate/edit/continue routes must never call
    retention.maintenance_if_due — only GET /v1/conversations (chained onto
    db_backup's own call site) does."""
    from app.routers import conversations as conversations_router

    calls: list[bool] = []
    # Pin backup_if_due() to a deterministic "no backup happened" so
    # maintenance_if_due's own backup_just_ran argument is predictable,
    # independent of DB_BACKUP's real staleness state on a fresh test DB.
    monkeypatch.setattr(conversations_router.db_backup, "backup_if_due", lambda: None)
    monkeypatch.setattr(
        conversations_router.retention,
        "maintenance_if_due",
        lambda backup_just_ran: calls.append(backup_just_ran) or None,
    )

    cid = client.post("/v1/conversations", json={"title": "t"}).json()["id"]
    assert calls == []  # POST /v1/conversations never touches maintenance

    client.get(f"/v1/conversations/{cid}/messages")
    assert calls == []  # a message-listing route never touches it either

    client.get("/v1/conversations")
    assert calls == [False]  # only the sidebar-load route does, and only once
