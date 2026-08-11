"""Cache effectiveness (app/cache_stats.py), and the two places that report
it.

The regression this exists for: the Usage panel had no cache figure at all,
so a model asked what this app lacked reported it had no visibility into
cache behaviour and proposed building the hit rate the weekly self-report
had been printing all along. Fixing that by computing it a second time in
the router would have set the two up to disagree, so both now share this
module — and the test at the bottom pins that they agree.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app import cache_stats, database, self_report


def _seed(owner: str | None = "alice") -> None:
    """Eight real calls and two cache hits: a 20% hit rate over ten requests."""
    for _ in range(8):
        database.record_spend(owner, "gpt-5", 100, 50, 0.01)
    database.record_avoided_cost(owner, "gpt-5", "response_cache_hit", 0.01)
    database.record_avoided_cost(owner, "gpt-5", "semantic_cache_hit", 0.01)


def _by_model(owner: str | None = "alice", days: int = 7) -> list[dict[str, object]]:
    return database.usage_summary(owner, days)["by_model"]


def test_hits_are_counted_in_the_denominator(db_path: Path) -> None:
    """The decision worth pinning. A cache hit writes no spend row, so
    dividing by billed calls alone would report 2/8 = 25% here — and would
    climb toward 100% precisely as the cache stopped working."""
    _seed()
    stats = cache_stats.summarize("alice", 7, _by_model())
    assert stats["total_requests"] == 10
    assert stats["exact_hit_rate"] == 0.1
    assert stats["semantic_hit_rate"] == 0.1


def test_counts_are_split_by_kind(db_path: Path) -> None:
    _seed()
    stats = cache_stats.summarize("alice", 7, _by_model())
    assert stats["exact_hits"] == 1
    assert stats["semantic_hits"] == 1


def test_avoided_cost_sums_every_reason(db_path: Path) -> None:
    """Including the free lane, which is not a cache hit and so is absent
    from the rates above but is still cost this owner did not pay."""
    _seed()
    database.record_avoided_cost("alice", "llama", "free_tier", 0.05)
    stats = cache_stats.summarize("alice", 7, _by_model())
    assert stats["avoided_cost_usd"] == 0.07
    assert stats["total_requests"] == 10  # free-lane calls are not cache hits


def test_empty_window_reports_no_rate_rather_than_zero(db_path: Path) -> None:
    """None, not 0.0 — a 0% would read as a cache that is on and never
    hitting, which is a different and much worse thing to see."""
    stats = cache_stats.summarize("alice", 7, [])
    assert stats["total_requests"] == 0
    assert stats["exact_hit_rate"] is None
    assert stats["semantic_hit_rate"] is None


def test_is_scoped_by_owner(db_path: Path) -> None:
    _seed("alice")
    assert cache_stats.summarize("bob", 7, _by_model("bob"))["total_requests"] == 0


def test_usage_endpoint_exposes_the_cache_block(
    client: TestClient, db_path: Path
) -> None:
    _seed(owner=None)
    body = client.get("/v1/usage?days=7").json()
    assert body["cache"]["total_requests"] == 10
    assert body["cache"]["exact_hits"] == 1
    assert body["cache"]["semantic_hit_rate"] == 0.1


def test_usage_endpoint_and_weekly_report_agree(db_path: Path) -> None:
    """The whole reason this is one shared module: two callers, one set of
    numbers. A future edit to either that changes the answer breaks here."""
    _seed()
    stats = cache_stats.summarize("alice", 7, _by_model())
    report = self_report.compile_stats("alice", days=7)
    assert report["total_requests"] == stats["total_requests"]
    assert report["exact_cache_hit_rate"] == stats["exact_hit_rate"]
    assert report["semantic_cache_hit_rate"] == stats["semantic_hit_rate"]
    assert report["avoided_cost_usd"] == stats["avoided_cost_usd"]
