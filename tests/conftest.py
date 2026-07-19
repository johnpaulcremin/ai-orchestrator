from __future__ import annotations

import os

# Set a dummy key before any app module is imported so load_dotenv() (called at
# app import time, override=False) cannot inject a real key from .env.
os.environ["OPENAI_API_KEY"] = "test-key"

from collections.abc import Iterator  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic: dummy API key, auth disabled, no model overrides."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
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
