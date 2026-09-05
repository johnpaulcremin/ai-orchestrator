"""The version is declared twice and released in a third place; keep them agreeing.

docs/development.md's Releasing steps bump app/main.py's ``FastAPI(version=...)``
and frontend/package.json's ``"version"`` together, to the value of the new
CHANGELOG heading. Four releases (v0.1.0 .. v0.4.0) went out with the changelog
and the git tag moving and both version strings still reading 0.1.0 -- so the
running app answered "what version is this?" wrongly for every one of them,
which is the one job the string has. A checklist step nobody is reminded of
is not a step; this makes the lockstep a CI failure instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent


def _latest_changelog_release() -> str:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## \[(\d+\.\d+\.\d+)\]", text, re.MULTILINE)
    assert match, "CHANGELOG.md has no released `## [X.Y.Z]` heading"
    return match.group(1)


def test_backend_and_frontend_declare_the_same_version() -> None:
    package = json.loads(
        (REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    assert app.version == package["version"]


def test_package_lock_carries_the_same_version() -> None:
    # `npm ci` tolerates a stale root version here, so nothing else would
    # notice it drifting from package.json.
    lock = json.loads(
        (REPO_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )
    assert lock["version"] == app.version
    assert lock["packages"][""]["version"] == app.version


def test_version_is_the_latest_changelog_release() -> None:
    # Between releases the declared version stays at the last cut; the
    # Unreleased section above it is what "since then" means. Moving
    # Unreleased under a new heading without bumping the two declarations
    # (or the reverse) fails here.
    assert app.version == _latest_changelog_release()
