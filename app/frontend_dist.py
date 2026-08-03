"""Locate the built frontend (frontend/dist), if it exists.

Serving the built SPA from the backend lets a single tunnel (e.g.
`tailscale serve --bg 8000`) reach the whole app, including on older mobile
browsers that can't run the untranspiled Vite dev server — see
docs/remote-access.md. FRONTEND_DIST_DIR overrides the default location,
mainly so tests can point at a small fixture dist without needing a real
build.
"""

from __future__ import annotations

import os
from pathlib import Path


def frontend_dist_dir() -> Path:
    override = (os.getenv("FRONTEND_DIST_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"
