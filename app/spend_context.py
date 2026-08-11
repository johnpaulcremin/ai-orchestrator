"""Which conversation the in-flight request belongs to, for spend attribution.

A conversation's displayed cost was summed from its saved MESSAGES, so every
call that spent money without producing one — a discarded regenerate, a
cancelled stream, an answer that came back empty — was invisible in it. One
real session showed $0.1014 in the footer against $0.5742 actually billed.

app/retry_attribution.py already covers part of this from the other end,
recording a failed attempt against its TURN, and its `record_failed_attempt`
docstring states the limit that remains: a failure on a turn with no answer
yet records nothing, because "the money reached spend_log, where nothing can
tie it to the turn that spent it." This is that missing tie, one level up — a
conversation rather than a turn, which needs no anchor row to exist first.

Fixing that means the spend log has to know which conversation a call belongs
to. That fact is ambient request metadata, not an input to answering: routing,
tool gating, and the model call itself have no use for it, and threading it
through `run_orchestrator`/`stream_orchestrator`/`run_workflow` as a parameter
would have put a display-only concern in the signature of every answering
function (and in every test double of them). A ContextVar keeps it beside the
request instead of inside the pipeline.

Set it at the edge — the routers, which know the conversation — via
`conversation_scope`. Read it where money is recorded: `budget.reserve` and
`orchestrator_spend._record_spend`. Outside a conversation request (the
stateless /v1/ask endpoints, internal calls) it is simply None and spend is
logged unattributed, exactly as before.

THREADS: a ContextVar is per-thread, and the streaming path runs its
orchestrator generator in a worker thread (see routers/messages/_shared.py).
That worker sets the scope itself from the conversation_id it is already
handed — a copied context would be the alternative, but an explicit set at the
top of the worker is far easier to follow.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_conversation_id: ContextVar[int | None] = ContextVar(
    "spend_conversation_id", default=None
)


def current_conversation_id() -> int | None:
    """The conversation the in-flight request belongs to, or None."""
    return _conversation_id.get()


@contextmanager
def conversation_scope(conversation_id: int | None) -> Iterator[None]:
    """Attribute every billable call made inside this block to `conversation_id`.

    Always resets on exit, including on an exception — a leaked scope would
    misattribute a later request's spend on the same thread.
    """
    token = _conversation_id.set(conversation_id)
    try:
        yield
    finally:
        _conversation_id.reset(token)
