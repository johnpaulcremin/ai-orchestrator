"""Baseline security response headers, applied to every response this
backend sends — a defense-in-depth layer independent of anything a client
does right, following the general hardening pass this app went through
alongside app/context_fencing.py.

Four headers, every response:

- `Content-Security-Policy` — this backend only ever serves JSON (never
  HTML the browser would render as a page), so the tightest policy that
  doesn't break anything is `default-src 'none'`: no script/style/image/
  connect/frame source is ever legitimately needed FROM an API response
  itself. This mitigates a MIME-confusion attack (a browser somehow induced
  to render a JSON response as HTML/script) rather than a realistic normal
  request — the frontend's OWN CSP (see frontend/nginx.conf) is what
  actually governs the page a user's browser renders.
- `X-Frame-Options: DENY` — belt-and-suspenders alongside CSP's
  `frame-ancestors` (which frontend/nginx.conf sets); harmless to also set
  here even though a bare JSON response was never embeddable as a frame in
  any meaningful way.
- `Referrer-Policy: strict-origin-when-cross-origin` — never leaks a full
  URL (which could carry a share token in its path) to a cross-origin
  referrer target; same-origin requests still get the full path.
- `X-Content-Type-Options: nosniff` — stops a browser from re-interpreting
  a JSON response as something else based on sniffed content, closing off
  the same MIME-confusion angle the CSP above targets from the other side.

ONE DOCUMENTED ALLOWANCE: FastAPI's auto-generated API docs (`/docs`,
`/redoc`, `/openapi.json`) load their own JS/CSS from a public CDN
(`cdn.jsdelivr.net`) — a strict `default-src 'none'` CSP would break them
outright. Since these are an operator-facing dev tool, not anything the
shipped frontend depends on, they're simply exempted from the CSP header
by path rather than loosening the policy for every other response to
accommodate a third-party CDN. The other three headers still apply there.

The public share route (`GET /v1/shared/{token}`) additionally gets
`X-Robots-Tag: noindex` — so a leaked share link doesn't end up crawled/
indexed. Applied here by path PREFIX (`/v1/shared/`), not set in
app/routers/shares.py's own handler via an injected `Response` — a header
set that way does not survive a raised `HTTPException` (FastAPI builds a
fresh response for the exception handler, discarding the injected
Response's headers), so the 404-for-an-invalid-token case would silently
lose it. Middleware wraps the actual outgoing response either way, success
or 404. Every OTHER route already requires auth or an owner check, so
there's nothing there worth a search engine finding regardless — adding
noindex universally would be a no-op everywhere except this one genuinely
public route.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

# See module docstring's "ONE DOCUMENTED ALLOWANCE" note.
_CSP_EXEMPT_PATHS = {"/docs", "/redoc", "/openapi.json"}

# See module docstring's X-Robots-Tag note.
_NOINDEX_PATH_PREFIX = "/v1/shared/"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if request.url.path not in _CSP_EXEMPT_PATHS:
            response.headers["Content-Security-Policy"] = "default-src 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith(_NOINDEX_PATH_PREFIX):
            response.headers["X-Robots-Tag"] = "noindex"
        return response
