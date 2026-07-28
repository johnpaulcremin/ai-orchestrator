"""Self-updating model/pricing catalog: pulls LiteLLM's published pricing
feed (model_prices_and_context_window.json) instead of relying solely on
the hand-maintained _DEFAULT_PRICING table in app/usage.py, and tracks which
model names are newly seen since the last successful sync.

Opt-in (MODEL_CATALOG_SYNC=false by default, a runtime-editable feature flag
like any other — see app/settings.py) since this is the one thing in this
app that ever calls a server OTHER than a configured LLM provider — every
other network call here goes to an API you deliberately pointed the app at.
Off by default keeps that boundary intact until you choose otherwise.

Layered pricing precedence, applied in usage._pricing() (lowest to highest):
  1. This module's cached catalog — auto-fetched, broad real-world coverage,
     but may be stale, missing, or simply not have an entry for a model this
     app's own defaults/examples point at.
  2. usage._DEFAULT_PRICING — hand-curated, deliberately bundled so this
     app's own tier defaults always resolve to a real number even offline
     or before the first sync.
  3. MODEL_PRICING — an explicit user override, which always wins over both.
A model absent from all three stays unpriced (or hits the Ollama-is-free
special case in usage.estimate_cost), exactly like today.

No background scheduler — there isn't one in this app, and adding a cron-like
thread just for this would be a lot of new machinery for a single feature.
Instead: sync_now() only ever runs when explicitly requested (POST
/v1/model-catalog/sync, wired to a "Sync now" button in Settings), or via
sync_if_stale() from the GET status endpoint — itself only ever hit when a
human opens the Settings panel, never on the hot answering path and never
during app startup, so a fresh install (and every test run) never makes
this network call unless a test explicitly opts in and mocks it.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from . import database
from .settings import bool_setting
from .telemetry import logger

_DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
_DEFAULT_SYNC_INTERVAL_HOURS = 24
_FETCH_TIMEOUT_SECONDS = 10.0


def enabled() -> bool:
    """Opt-in: MODEL_CATALOG_SYNC=true (env, or a saved Settings override —
    same override > env > default chain as any other feature flag)."""
    return bool_setting("MODEL_CATALOG_SYNC", False)


def _feed_url() -> str:
    return (os.getenv("MODEL_CATALOG_FEED_URL") or "").strip() or _DEFAULT_FEED_URL


def sync_interval_hours() -> int:
    raw = (os.getenv("MODEL_CATALOG_SYNC_INTERVAL_HOURS") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_SYNC_INTERVAL_HOURS
    except ValueError:
        return _DEFAULT_SYNC_INTERVAL_HOURS
    return value if value > 0 else _DEFAULT_SYNC_INTERVAL_HOURS


def _parse_feed(raw_json: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Extract {model: (input_per_1M, output_per_1M)} from LiteLLM's feed
    shape — per-TOKEN USD costs, converted to this app's per-1M convention.
    Skips any entry missing a plain numeric input/output token cost (e.g.
    embedding/image/audio/moderation entries priced by a different unit),
    since those would otherwise silently poison this app's token-based
    estimate_cost() math.
    """
    pricing: dict[str, tuple[float, float]] = {}
    for model, entry in raw_json.items():
        if not isinstance(entry, dict):
            continue
        input_cost = entry.get("input_cost_per_token")
        output_cost = entry.get("output_cost_per_token")
        if not isinstance(input_cost, (int, float)) or not isinstance(
            output_cost, (int, float)
        ):
            continue
        if input_cost < 0 or output_cost < 0:
            continue
        pricing[model] = (input_cost * 1_000_000, output_cost * 1_000_000)
    return pricing


def sync_now() -> dict[str, Any]:
    """Fetch the live feed and persist it, unconditionally (ignores
    staleness) — the "Sync now" button's action. Never raises: any failure
    (network, parse, DB) leaves the previously-cached catalog untouched and
    is reported back via the `error` field rather than losing good data.
    A no-op returning the current (unsynced) status when the feature is off.
    """
    if not enabled():
        return status()
    try:
        response = httpx.get(_feed_url(), timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        raw_json = response.json()
        if not isinstance(raw_json, dict):
            raise ValueError("feed did not return a JSON object")
    except Exception:
        logger.warning("model_catalog.sync_failed", exc_info=True)
        result = status()
        result["error"] = "Sync failed — see server logs. Last known catalog kept."
        return result

    pricing = _parse_feed(raw_json)
    model_names = sorted(pricing.keys())

    try:
        previous = database.get_model_catalog()
    except sqlite3.Error:
        previous = None
    previous_names = set(json.loads(previous["model_names_json"])) if previous else None
    # First-ever sync has nothing to diff against — reporting every model in
    # the feed as "new" would be a wall of noise, not a useful notice.
    new_models = (
        sorted(set(model_names) - previous_names) if previous_names is not None else []
    )

    try:
        database.set_model_catalog(
            pricing_json=json.dumps({m: list(p) for m, p in pricing.items()}),
            model_names_json=json.dumps(model_names),
            new_models_json=json.dumps(new_models),
            model_count=len(model_names),
        )
    except sqlite3.Error:
        logger.exception("model_catalog.persist_failed")
        result = status()
        result["error"] = "Sync succeeded but saving the result failed."
        return result

    return status()


def sync_if_stale() -> dict[str, Any]:
    """Read-mostly status check: triggers sync_now() only when enabled AND
    the cached catalog is missing or older than sync_interval_hours() —
    otherwise just reads the cache. This is what GET /v1/model-catalog
    calls, so opening the Settings panel is what "on a schedule" actually
    means here (see module docstring — there's no cron)."""
    if not enabled():
        return status()
    current = status()
    if current["stale"]:
        return sync_now()
    return current


def _is_stale(fetched_at: str) -> bool:
    """Whether a "YYYY-MM-DD HH:MM:SS" UTC timestamp (SQLite's
    CURRENT_TIMESTAMP format) is older than sync_interval_hours(). Any
    parse failure counts as stale — a corrupt timestamp shouldn't wedge the
    catalog into never refreshing again."""
    try:
        fetched = datetime.strptime(fetched_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return True
    return datetime.now(timezone.utc) - fetched > timedelta(hours=sync_interval_hours())


def status() -> dict[str, Any]:
    """The cached catalog's status — DB read only, never touches the
    network. `stale` is True whenever enabled() and (never synced, or the
    last sync is older than sync_interval_hours())."""
    is_enabled = enabled()
    try:
        row = database.get_model_catalog()
    except sqlite3.Error:
        row = None
    if row is None:
        return {
            "enabled": is_enabled,
            "synced_at": None,
            "model_count": 0,
            "new_models": [],
            "stale": is_enabled,
        }
    return {
        "enabled": is_enabled,
        "synced_at": row["fetched_at"],
        "model_count": int(row["model_count"]),
        "new_models": json.loads(row["new_models_json"]),
        "stale": is_enabled and _is_stale(str(row["fetched_at"])),
    }


def cached_pricing() -> dict[str, tuple[float, float]]:
    """The last-synced catalog's pricing table — DB read only, never
    touches the network. {} when disabled or never synced, so callers can
    layer it in unconditionally without a separate enabled() check."""
    if not enabled():
        return {}
    try:
        row = database.get_model_catalog()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    raw: dict[str, list[float]] = json.loads(row["pricing_json"])
    return {model: (values[0], values[1]) for model, values in raw.items()}
