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


@pytest.fixture(autouse=True)
def _test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test hermetic: dummy API key, auth disabled by default."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


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
