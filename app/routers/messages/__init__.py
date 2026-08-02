"""Message-level and conversation-scoped ask/regenerate/edit/continue
endpoints, plus the context-building helpers they share. See
app/routers/conversations.py for conversation CRUD, and app/routers/ask.py
for the stateless (context-free) /v1/ask, /v1/compare, /v1/estimate.

Split across sibling modules by route family (crud/ask/regenerate/edit/
action_resolution) plus a shared streaming/dedup engine (_shared) — pure
organization, not a behavior change; every route path, name, schema, and
this package's own public names are unchanged from when this was a single
file. Importing the submodules below (for their route-registration side
effect) is what makes every endpoint exist on `router` exactly as before.

MONKEYPATCH-CRITICAL: `run_orchestrator`, `stream_orchestrator`,
`run_workflow`, `stream_workflow`, `post_webhook`, and `add_message` are
imported here (module level, same as before the split) specifically so
`monkeypatch.setattr(app.routers.messages, "run_orchestrator", fake)` (used
across dozens of existing tests) keeps working unchanged. Every submodule
below reads these SIX names via a qualified `_messages.<name>` reference
(see e.g. messages/ask.py) resolved at CALL time against THIS module's own
attributes, rather than binding a bare name at import time — the same
"read the current attribute, not a snapshot" technique
app/orchestrator_summarize.py's `_run_summary_call` already uses (see its
own comment) to keep a monkeypatch on a name effective regardless of which
module actually calls it. Every other import here (schemas, database CRUD,
encode helpers, ask_support helpers) is never monkeypatched via this
module's path, so submodules import those directly and normally.
"""

from __future__ import annotations

from ... import memory, request_registry  # noqa: F401 (re-exported for submodules)
from ...actions import post_webhook  # noqa: F401
from ...audio_ingestion import resolve_audio_attachments  # noqa: F401
from ...spreadsheet_ingestion import resolve_xlsx_attachments  # noqa: F401
from ...ask_support import (  # noqa: F401
    _is_context_free,
    _is_generic_title,
    _library_stage_timing,
    _memory_stage_timing,
    _pinned_ask_request,
    _recall_library,
    _recall_memory,
    _title_from_question,
)
from ...auth import current_owner  # noqa: F401
from ...context_builder import (  # noqa: F401
    build_context_prompt,
    build_context_prompt_with_cache_split,
    build_recent_history_snippet,
)
from ...database import (  # noqa: F401
    add_message,
    append_to_message,
    claim_pending_action,
    delete_message,
    delete_messages_after,
    delete_messages_from,
    get_conversation,
    get_message,
    list_bookmarked_messages,
    list_messages,
    set_action_status,
    set_message_bookmarked,
    set_message_feedback,
    update_conversation_title,
)
from ...orchestrator import run_orchestrator, stream_orchestrator  # noqa: F401
from ...ratelimit import limiter, rate_limit_value  # noqa: F401
from ...telemetry import logger  # noqa: F401
from ...schemas import (  # noqa: F401
    ActionConfirmRequest,
    ActionResult,
    AskRequest,
    AskResponse,
    BookmarkedMessage,
    FileAttachment,
    MessageBookmark,
    MessageFeedback,
    MessageOut,
    MessageRestoreRequest,
    Mode,
    RegenerateRequest,
)
from ...workflow import run_workflow, stream_workflow  # noqa: F401
from ..deps import (  # noqa: F401
    _encode_academic_results,
    _encode_action,
    _encode_audio,
    _encode_code_results,
    _encode_fact_checks,
    _encode_files,
    _encode_images,
    _encode_library_sources,
    _encode_math_results,
    _encode_sources,
    _encode_workflow_steps,
    _owned_or_404,
    router,
)

from ._shared import (  # noqa: F401
    _QUEUE_DONE,
    _dedup_or_call,
    _replay_duplicate_stream,
    _run_ask_stream_worker,
    _run_workflow_stream_worker,
    _stream_and_persist,
    _stream_workflow_and_persist,
)

# Side-effect imports: each registers its routes onto the shared `router`
# from app.routers.deps, imported above — this is the ENTIRE reason these
# submodules need importing at all here (see this module's own docstring).
from . import action_resolution, ask, crud, edit, regenerate  # noqa: E402,F401
