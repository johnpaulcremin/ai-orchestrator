from __future__ import annotations

import os

# Set a dummy key before any app module is imported so load_dotenv() (called at
# app import time, override=False) cannot inject a real key from .env.
os.environ["OPENAI_API_KEY"] = "test-key"

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import ratelimit  # noqa: E402
from app.database import init_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.routing import ALL_CATEGORIES  # noqa: E402

# Model-selection env vars that could leak from a developer's .env (loaded at
# import) and make routing tests non-hermetic — e.g. a MODEL_CODING override
# changing what an auto-routing test resolves to.
_MODEL_ENV_VARS = [
    "OPENAI_MODEL",
    "OPENAI_MODEL_ROUTER",
    "OPENAI_MODEL_BUDGET",
    "OPENAI_MODEL_FAST",
    "OPENAI_MODEL_SMART",
    "OPENAI_MODEL_FALLBACK",
    *(f"MODEL_{category.upper()}" for category in ALL_CATEGORIES),
]


@pytest.fixture(autouse=True)
def _test_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic: dummy API key, auth disabled, no model overrides.

    Also pins DATABASE_PATH to a throwaway file so routing (which now reads the
    settings table) can never pick up a developer's real ai_orchestrator.db.
    Tests that need a schema-initialised DB request the `db_path`/`client`
    fixtures, which override this with their own initialised file.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    # A developer's real .env can set a personal-preference JWT_EXPIRY_DAYS;
    # scrub it so token-lifetime tests are hermetic, same reasoning as the
    # other auth-adjacent vars here.
    monkeypatch.delenv("JWT_EXPIRY_DAYS", raising=False)
    monkeypatch.delenv("ALLOW_SETTINGS_WRITE", raising=False)
    # A developer's real .env can set this (e.g. for their own deployment);
    # load_dotenv()'s override=False means it only ever fills in a var that
    # ISN'T already in os.environ, so once dotenv injects it at import time
    # it stays for the whole process unless a test explicitly clears it —
    # scrub it here so registration-flow tests are hermetic regardless of
    # local .env contents, same reasoning as the other auth-adjacent vars
    # above.
    monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
    # Informational only (app/main.py's _warn_if_exposed_without_auth) — a
    # developer's real .env could set this for their own Tailscale setup.
    monkeypatch.delenv("BIND_HOST", raising=False)
    # Caching off by default so tests exercise the model path; cache tests opt in.
    monkeypatch.setenv("RESPONSE_CACHE", "false")
    monkeypatch.delenv("RESPONSE_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("RESPONSE_CACHE_MAX_ENTRIES", raising=False)
    # History summarization off by default (it would make a router call); the
    # summary tests opt in and inject a fake summarizer.
    monkeypatch.setenv("SUMMARIZE_HISTORY", "false")
    monkeypatch.delenv("SUMMARY_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "autouse.db"))
    # Default to no built frontend so tests are hermetic regardless of whether
    # a developer has run `npm run build` locally (frontend/dist is
    # gitignored); tests/test_frontend_serving.py opts into a fixture dist.
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path / "no-frontend-dist"))
    # No spend cap by default; the budget tests opt in.
    monkeypatch.delenv("DAILY_BUDGET_USD", raising=False)
    # The ask-endpoint limiter is opt-in (RATE_LIMIT) and already defaults off
    # via rate_limiting_enabled(); scrub the env var too so a developer's real
    # .env (e.g. RATE_LIMIT=60/minute) can't leak into a test run and make an
    # otherwise-unrelated test flaky. The auth_limiter is intentionally ALWAYS
    # on in production (see app/ratelimit.py) regardless of any env var, so it
    # must be force-disabled here explicitly; test_ratelimit.py's dedicated
    # tests re-enable both limiters to exercise them.
    monkeypatch.delenv("RATE_LIMIT", raising=False)
    monkeypatch.delenv("AUTH_RATE_LIMIT", raising=False)
    # Retention settings (app/retention.py) default to keeping detail
    # forever-ish (365 days) / never expiring a share link — scrub so a
    # developer's real .env can't make an unrelated test flaky.
    monkeypatch.delenv("RETENTION_DAYS_DETAIL", raising=False)
    monkeypatch.delenv("SHARE_EXPIRY_DAYS", raising=False)
    monkeypatch.setattr(ratelimit.auth_limiter, "enabled", False)
    # No revocation.clear() equivalent needed anymore: revocation state is
    # DB-backed with no module-level state (see app/revocation.py), so the
    # per-test DATABASE_PATH above isolates it like every other table.
    for name in _MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the app at a throwaway sqlite file and initialise the schema."""
    path = tmp_path / "test_ai_orchestrator.db"
    monkeypatch.setenv("DATABASE_PATH", str(path))
    init_db()
    return path


@pytest.fixture()
def client(db_path: Path) -> Iterator[TestClient]:
    with TestClient(fastapi_app) as test_client:
        yield test_client
