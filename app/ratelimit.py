from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_enabled() -> bool:
    return bool((os.getenv("RATE_LIMIT") or "").strip())


def rate_limit_value() -> str:
    """
    Limit applied to the expensive ask endpoints, read at request time.

    Uses the standard slowapi syntax, e.g. "60/minute" or "5/second". When
    RATE_LIMIT is unset the limiter is disabled, so this default is only used
    as the decorator's placeholder value.
    """
    return (os.getenv("RATE_LIMIT") or "").strip() or "60/minute"


# Keyed by client IP. Disabled unless RATE_LIMIT is set, so local single-user
# setups are unaffected by default.
limiter = Limiter(
    key_func=get_remote_address,
    enabled=_rate_limit_enabled(),
    default_limits=[],
)
