"""Per-request identity and stage timing: the request id that ties log lines
together, and the StageTimer every latency figure in an answer's `notes` is
built from.

Local and free, and separate from app/observability.py on purpose — that
module is opt-in OpenTelemetry export, this one always runs. A deployment
with no tracing backend still gets a request id and a per-stage breakdown
(routing, moderation, retrieval, the provider call), because "which stage
was slow" is the first question asked about a slow answer and it should not
require infrastructure to answer.

The timings are threaded into the answer itself rather than only logged, so
a user looking at a slow response sees where the time went without anyone
reading a log file.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger("ai_orchestrator")


@dataclass(frozen=True)
class RequestMeta:
    request_id: str
    started_ms: int


def new_request_meta() -> RequestMeta:
    return RequestMeta(
        request_id=str(uuid.uuid4()),
        started_ms=int(time.time() * 1000),
    )


def elapsed_ms(meta: RequestMeta) -> int:
    return int(time.time() * 1000) - meta.started_ms


class StageTimer:
    """Per-stage latency breakdown for one request's lifecycle — distinct
    from `elapsed_ms`'s single cumulative "how long has this whole request
    taken" figure. The ask path stacks several independent stages before any
    token streams back (moderation check, cross-conversation memory embed,
    semantic-cache lookup, routing/classification, budget reservation, the
    actual model call) — this exists to answer "which ONE of those is
    actually slow," not just "the request took 900ms."

    Two ways a stage's duration gets in:
      - `mark(stage)`: for a stage timed AFTER this timer exists (i.e. inside
        orchestrator.py, which owns the RequestMeta) — records elapsed_ms at
        call time; the stage's own duration is the delta from the previous
        mark (or from request start, for the first one).
      - `record(stage, duration_ms)`: for a stage timed by the CALLER before
        this timer/request even existed (e.g. cross-conversation memory's
        embedding call, measured in routers/messages.py before
        run_orchestrator is invoked) — the duration is used as-is, not
        derived from a delta.
    """

    def __init__(self, meta: RequestMeta) -> None:
        self._meta = meta
        self._external: list[tuple[str, int]] = []
        self._marks: list[tuple[str, int]] = []

    def record(self, stage: str, duration_ms: int) -> None:
        self._external.append((stage, duration_ms))

    def mark(self, stage: str) -> None:
        self._marks.append((stage, elapsed_ms(self._meta)))

    def stages(self) -> list[tuple[str, int]]:
        """[(stage, duration_ms), ...] in the order recorded — externally
        `record`-ed stages first (they precede this timer's own clock),
        then each `mark`-ed stage's own duration (not a running total)."""
        result = list(self._external)
        previous = 0
        for stage, cumulative in self._marks:
            result.append((stage, cumulative - previous))
            previous = cumulative
        return result

    def summary(self) -> str:
        """A compact 'stage=Nms stage2=Nms' string for a single log line."""
        return " ".join(f"{stage}={duration}ms" for stage, duration in self.stages())
