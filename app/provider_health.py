"""Remembers which models just failed to answer, so the next request does not
pay the same timeout again — a process-local circuit breaker with exponential
backoff.

The gap this closes. app/local_health.py TCP-probes every configured local
model ONCE, at startup, and warns. That catches the server that was already
down when the app booted; it says nothing about the one that dies (or is
never started) afterwards. Seen live: Ollama unreachable while
`OLLAMA_API_BASE` pointed at a container-only hostname, so EVERY budget-tier
question spent the full connect timeout failing, then silently failed over to
a PAID model. The answers were correct and the routing notes were honest; the
only signal was the bill. Nothing in the app remembered, between one request
and the next, that the model had just failed — so request 50 paid exactly the
same latency penalty as request 1.

What trips the breaker is deliberately narrow: only the two reasons that mean
"this model could not be reached, and waiting might fix it" —
`connection_error` and `timeout` (see app/fallback_reason.py). The others are
excluded because a breaker is the wrong instrument for them:

  * quota_cooldown already has a better mechanism: free_tier.exhaust_for_today
    cools a throttled free model for the rest of the UTC day.
  * provider_error covers real API failures that are often request-specific
    (a malformed payload, an unsupported parameter) — tripping on those would
    take a working provider out of service over one bad request.
  * context_length_exceeded and tool_unsupported are properties of the
    REQUEST, not the model's reachability: the same model answers the next,
    smaller question fine.
  * budget_refusal never reached a provider at all.

An authentication failure never arrives here either: the orchestrator has its
own branch for one, which stops with "check <KEY_ENV>" instead of falling
back, because waiting cannot fix a wrong key.

Two uses, both in app/orchestrator.py:

  1. Before dispatch, an unhealthy PRIMARY is swapped for its first healthy
     fallback (see `_apply_health_override`). This is where the latency and
     the surprise cost actually go away: the dead model is skipped outright
     rather than being tried, timing out, and then falling back anyway.
  2. Inside the fallback loop, unhealthy CANDIDATES are tried last rather
     than first — never dropped. A known-bad candidate still beats no
     candidate when every option looks bad, and the breaker is an
     optimisation, not a veto.

Deliberately process-local and unpersisted, like image_processing's Tesseract
probe: a restart is the operator saying "I changed something, try again", and
carrying a stale breaker across it would punish the very fix that resolved it.
"""

from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass

from .fallback_reason import CONNECTION_ERROR, TIMEOUT
from .telemetry import logger

# The reasons that mean "unreachable, and this might pass later". See the
# module docstring for why every other reason is excluded.
TRIPPING_REASONS = frozenset({CONNECTION_ERROR, TIMEOUT})

# Consecutive failures before the breaker opens. Two, not one: a single
# timeout to a hosted provider is ordinary noise, and skipping a healthy
# model over one blip would route a request to a worse tier for no reason.
# A genuinely dead local server fails every call, so it reaches two almost
# immediately — the case this exists for costs one extra request, once.
_DEFAULT_THRESHOLD = 2

# First cooldown, in seconds. Long enough to skip a burst of requests against
# a dead server, short enough that a server coming back up is noticed within
# a question or two.
_DEFAULT_COOLDOWN_SECONDS = 30.0

# Ceiling for the exponential growth. Past five minutes the breaker is no
# longer saving anything worth the risk of ignoring a recovered model.
_MAX_COOLDOWN_SECONDS = 300.0

# Cooldowns are spread by up to this fraction so that several models tripped
# by one outage do not all retry on the same tick.
_JITTER_FRACTION = 0.2


@dataclass
class _Breaker:
    failures: int = 0
    trips: int = 0
    open_until: float = 0.0


_state: dict[str, _Breaker] = {}
# Every mutation is a handful of integer writes, but streaming and
# non-streaming requests share this dict across threads, so the whole
# read-modify-write is guarded rather than relying on the GIL.
_lock = threading.Lock()


def _positive_float(env_var: str, default: float) -> float:
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _threshold() -> int:
    """PROVIDER_HEALTH_THRESHOLD, or the default. A value below 1 would open
    the breaker on a model that has not failed at all, so it is floored."""
    return max(1, int(_positive_float("PROVIDER_HEALTH_THRESHOLD", _DEFAULT_THRESHOLD)))


def _base_cooldown() -> float:
    return _positive_float("PROVIDER_HEALTH_COOLDOWN", _DEFAULT_COOLDOWN_SECONDS)


def _max_cooldown() -> float:
    return max(_base_cooldown(), _MAX_COOLDOWN_SECONDS)


def enabled() -> bool:
    """On by default. `PROVIDER_HEALTH=false` disables the breaker entirely —
    every model is reported healthy and nothing is ever skipped, which is
    exactly the behaviour that existed before this module."""
    raw = (os.getenv("PROVIDER_HEALTH") or "").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def cooldown_for(trips: int) -> float:
    """Seconds to stay open after the `trips`-th consecutive trip: the base
    doubled per trip, capped, then spread by up to ±20% of jitter.

    Exposed (and jitter-free at trips <= 0) so a test can assert the growth
    curve without reaching into the module's private state.
    """
    if trips <= 0:
        return 0.0
    base = _base_cooldown() * (2 ** (trips - 1))
    capped = min(base, _max_cooldown())
    jitter = capped * _JITTER_FRACTION
    # noqa: S311 — spreading retries, not generating a secret.
    return max(0.0, capped + random.uniform(-jitter, jitter))  # noqa: S311


def record_failure(model: str, reason: str) -> None:
    """Note that `model` failed with `reason`. Opens the breaker once the
    consecutive-failure threshold is reached; a reason outside
    TRIPPING_REASONS is ignored entirely (it does not even reset the count,
    since it says nothing about reachability)."""
    name = (model or "").strip()
    if not name or reason not in TRIPPING_REASONS or not enabled():
        return
    with _lock:
        breaker = _state.setdefault(name, _Breaker())
        breaker.failures += 1
        if breaker.failures < _threshold():
            return
        breaker.trips += 1
        breaker.failures = 0
        cooldown = cooldown_for(breaker.trips)
        breaker.open_until = time.monotonic() + cooldown
        trips = breaker.trips
    logger.warning(
        "provider_health.opened model=%s reason=%s cooldown=%.0fs trips=%d — "
        "skipping this model until it cools down; requests routed to it will "
        "go to a fallback instead",
        name,
        reason,
        cooldown,
        trips,
    )


def record_success(model: str) -> None:
    """`model` answered: close its breaker and forget the failure history.

    The trip count resets too, so a model that recovers starts from the base
    cooldown again rather than inheriting an old outage's backoff."""
    name = (model or "").strip()
    if not name:
        return
    with _lock:
        breaker = _state.get(name)
        if breaker is None or (
            breaker.failures == 0 and breaker.trips == 0 and breaker.open_until == 0.0
        ):
            return
        was_open = breaker.open_until > time.monotonic()
        _state.pop(name, None)
    if was_open:
        logger.info("provider_health.closed model=%s — answered again", name)


def is_unhealthy(model: str) -> bool:
    """True while `model`'s breaker is open. False for an unknown model, for
    an expired cooldown (the next call is the probe that decides whether it
    reopens), and always when the feature is switched off."""
    name = (model or "").strip()
    if not name or not enabled():
        return False
    with _lock:
        breaker = _state.get(name)
        if breaker is None:
            return False
        return breaker.open_until > time.monotonic()


def healthy_first(models: list[str]) -> list[str]:
    """`models` reordered so unhealthy ones come last, order otherwise kept.

    Reordering, not filtering: when every candidate looks unhealthy the list
    must still be tried — a model whose breaker is open might well answer,
    and returning nothing would turn a degraded request into a failed one.
    """
    if not enabled():
        return list(models)
    healthy = [m for m in models if not is_unhealthy(m)]
    unhealthy = [m for m in models if is_unhealthy(m)]
    return healthy + unhealthy


def snapshot() -> dict[str, float]:
    """{model: seconds remaining} for every currently-open breaker — for the
    status endpoint and tests. Expired entries are omitted, not reported as
    zero."""
    now = time.monotonic()
    with _lock:
        return {
            name: round(breaker.open_until - now, 1)
            for name, breaker in _state.items()
            if breaker.open_until > now
        }


def reset() -> None:
    """Forget every breaker. For tests, and for a Settings change that could
    have fixed whatever was failing."""
    with _lock:
        _state.clear()
