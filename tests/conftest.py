from __future__ import annotations

import os

# Set a dummy key before any app module is imported so load_dotenv() (called at
# app import time, override=False) cannot inject a real key from .env.
os.environ["OPENAI_API_KEY"] = "test-key"

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import revocation  # noqa: E402
from app.database import init_db  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.routing import ALL_CATEGORIES  # noqa: E402

# Model-selection env vars that could leak from a developer's .env (loaded at
# import) and make routing tests non-hermetic — e.g. a MODEL_CODING override
# changing what an auto-routing test resolves to.
_MODEL_ENV_VARS = [
    "OPENAI_MODEL",
    "OPENAI_MODEL_ROUTER",
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
    monkeypatch.delenv("ALLOW_SETTINGS_WRITE", raising=False)
    # Caching off by default so tests exercise the model path; cache tests opt in.
    monkeypatch.setenv("RESPONSE_CACHE", "false")
    monkeypatch.delenv("RESPONSE_CACHE_TTL_SECONDS", raising=False)
    monkeypatch.delenv("RESPONSE_CACHE_MAX_ENTRIES", raising=False)
    # History summarization off by default (it would make a router call); the
    # summary tests opt in and inject a fake summarizer.
    monkeypatch.setenv("SUMMARIZE_HISTORY", "false")
    monkeypatch.delenv("SUMMARY_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "autouse.db"))
    revocation.clear()  # in-memory revocation list must not leak between tests
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
