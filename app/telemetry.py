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