"""In-process, short-lived request_id -> result registry backing two related
guarantees for the streaming/non-streaming ask/regenerate/edit/continue/
workflow-send endpoints:

1. IDEMPOTENCY: the client attaches a generated request_id (a UUID) to every
   send, kept until acknowledged. If the same request_id arrives twice
   (double-click, a client-side retry after a slow/ambiguous response), the
   second arrival is joined to the first's in-flight or already-finished
   work instead of dispatching a second paid model call — see begin()/
   finish() below.

2. EXPLICIT ABORT (distinct from a bare disconnect): the Stop button calls
   POST /v1/requests/{request_id}/cancel, which flags the matching entry
   (see mark_aborted()). The worker thread actually running the answer
   (see app/routers/messages.py's _run_and_persist) checks this flag between
   provider-stream events and, if set, closes the orchestrator generator
   itself — triggering the SAME GeneratorExit-based reservation-release
   app/orchestrator.py's stream_orchestrator already does for a disconnect
   today. A bare socket drop with no matching cancel call never sets this
   flag, so the worker just keeps running to completion (see
   app/routers/messages.py's module-level note on the disconnect-propagation
   finding this whole feature is built on).

Deliberately NOT persisted (no new table): this is a short-lived, in-process
dedup window (~10 minutes), not a durable outbox — a server restart mid-flight
loses the mapping, same as any other in-memory cache in this app (response
cache, semantic cache). A request_id is meaningless across a restart anyway,
since the client always generates a fresh one per send.

Thread-safe: a background worker thread and the request-handling thread both
touch this module concurrently (see app/routers/messages.py).
"""

from __future__ import annotations

import threading
import time
from typing import Any

# How long a finished (or abandoned/never-finished) entry is kept before a
# sweep drops it — long enough to cover a slow reconnect/retry, short enough
# that this never grows into a real persistence concern. Matches the job
# spec's "~10 min" window.
_TTL_SECONDS = 600

_lock = threading.Lock()
_entries: dict[str, "_Entry"] = {}


class _Entry:
    """One request_id's shared state. `event` is set exactly once, when the
    ORIGINAL (first) caller's work finishes — a duplicate caller waits on it
    instead of doing its own work. `result` is whatever the endpoint wants a
    duplicate caller to receive back (an AskResponse-shaped dict for a
    non-streaming endpoint, a small "here's what already happened" summary
    for a streaming one — see app/routers/messages.py for each shape).
    `aborted` is set only by an explicit cancel call, never by a disconnect.
    """

    __slots__ = ("event", "result", "created_at", "aborted")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.created_at = time.monotonic()
        self.aborted = False


def _sweep_locked() -> None:
    cutoff = time.monotonic() - _TTL_SECONDS
    stale = [
        request_id
        for request_id, entry in _entries.items()
        if entry.created_at < cutoff
    ]
    for request_id in stale:
        del _entries[request_id]


def begin(request_id: str | None) -> tuple[_Entry | None, bool]:
    """Register the start of work for `request_id`.

    Returns (entry, is_new). `entry` is None when `request_id` is falsy
    (the caller didn't send one — always treated as new, no dedup tracked
    for it, same as today's behavior for any pre-existing client). When
    `is_new` is False, an entry for this exact request_id already exists:
    the caller must NOT dispatch a new model call — instead wait on
    `entry.event` (see `wait_for_result`) and use `entry.result`.
    """
    if not request_id:
        return None, True
    with _lock:
        _sweep_locked()
        existing = _entries.get(request_id)
        if existing is not None:
            return existing, False
        entry = _Entry()
        _entries[request_id] = entry
        return entry, True


def wait_for_result(entry: _Entry, timeout: float = 120.0) -> Any:
    """Block until the ORIGINAL caller's work finishes (or `timeout`
    elapses — a generous ceiling well past any real answer's latency, so a
    duplicate caller is never left hanging indefinitely if the original
    somehow never reaches finish()). Returns `entry.result`, or None on
    timeout (the caller decides how to degrade — see the route handlers)."""
    if entry.event.wait(timeout):
        return entry.result
    return None


def finish(entry: _Entry | None, result: Any) -> None:
    """Publish the result and wake every caller waiting in wait_for_result.
    A no-op when `entry` is None (an untracked, request_id-less call)."""
    if entry is None:
        return
    entry.result = result
    entry.event.set()


def mark_aborted(request_id: str) -> bool:
    """Flag `request_id`'s in-flight work as explicitly cancelled (the Stop
    button, never a disconnect — see module docstring). Returns False when
    there's nothing in-flight to mark: no such request_id, or it already
    finished (cancelling a completed answer is a no-op, not an error)."""
    with _lock:
        entry = _entries.get(request_id)
    if entry is None or entry.event.is_set():
        return False
    entry.aborted = True
    return True


def is_aborted(entry: _Entry | None) -> bool:
    return bool(entry is not None and entry.aborted)


def _reset_for_tests() -> None:
    """Test-only: clear all state between tests so one test's request_ids
    can never leak into another's (see tests/test_request_idempotency.py)."""
    with _lock:
        _entries.clear()
