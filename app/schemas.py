"""Every request and response shape the API accepts or returns, as Pydantic
models that actually validate rather than merely describe.

The caps here are the app's real input boundary: question length, attachment
counts, image and file sizes, chat-message counts, import sizes. They exist
so a pathological payload is refused at the edge with a 422, before it can
reach a budget reservation or a provider call — validation is the cheapest
place to say no.

Closed sets are Literals and Enums (Mode, the moderation and reason labels),
so an unknown value fails at parse time instead of flowing through routing
as a string nobody matched. Attachment validators additionally check the
data-URI prefix and decode-ability, since "it is a string" is not a useful
guarantee about something that will be handed to an image pipeline.

Response models are equally deliberate: a field that can be genuinely absent
is typed `| None` with a default rather than being omitted, so a client can
tell "not measured" from "measured as zero" — the same distinction
app/usage.py draws between an unpriced model and a free one, carried through
to the wire.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .settings import MAX_PROMPT_LEN, validate_model_value


def _clean_forced_model(value: str | None) -> str | None:
    """Validate an optional forced-model name; '' / whitespace -> None."""
    if value is None:
        return None
    cleaned = validate_model_value(value)  # raises ValueError on a malformed name
    return cleaned or None


class Mode(str, Enum):
    auto = "auto"
    fast = "fast"
    smart = "smart"
    budget = "budget"
    workflow = "workflow"


# Vision input attachments: at most this many per message, and each capped in
# size (base64 chars, ~9MB raw at the 4/3 encoding overhead) — a defensive
# limit against a client sending a pathologically large payload, not a
# meaningful constraint on ordinary photos/screenshots.
_MAX_INPUT_IMAGES = 4
_MAX_INPUT_IMAGE_CHARS = 12_000_000
# Only a data: URL is accepted (never a bare http(s) URL): passing an arbitrary
# remote URL through as `image_url` would have the PROVIDER's servers fetch it
# on our behalf — an SSRF vector via a third party. Restricting to base64 data
# closes that off at the schema layer.
_DATA_IMAGE_URL_RE = re.compile(
    r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/]+=*$"
)

# Generated video, arriving back through Import/Restore. Never user input — a
# clip is only ever produced by app/video_generation.py — but an import body is
# untrusted JSON like any other, and this value lands in a rendered
# `<video src>`. A bare-URL form would be an SSRF vector the same way an image
# one would; a `javascript:` form would be worse. Only base64 data passes, for
# the same reason and at the same layer.
# `\Z`, not `$`: in Python `$` also matches just before a trailing newline,
# so "data:video/mp4;base64,aaa\n" would pass. Nothing can follow that
# newline, so it was not exploitable — but the anchor should mean what it
# looks like it means.
_DATA_VIDEO_URL_RE = re.compile(r"\Adata:video/mp4;base64,[A-Za-z0-9+/]+=*\Z")
# An ordinary ask renders at most one clip. A WORKFLOW can render more: each of
# its steps runs the full single-ask pipeline, so several may each match the
# trigger, and app/workflow.py's artefact bag collects them all onto the final
# message. The cap is therefore workflow.py's own hard step ceiling — anything
# lower would let an answer be PERSISTED in a shape its own export/import
# round-trip rejects, which is how this was found. Sized against the ~10MB
# ceiling app/video_generation.py enforces on the bytes, base64'd (+~33%).
_MAX_VIDEOS = 6
_MAX_VIDEO_CHARS = 14_000_000


# Document attachments (PDF, plain text, or .xlsx): at most this many per
# message, each capped in size (base64 chars, ~15MB raw at the 4/3 encoding
# overhead). Deliberately a small, exact-match mime allowlist (not "any
# file") — these are the only types this app's document path knows how to
# handle: PDF and plain text pass through to the provider-side document
# blocks unchanged (see providers.py); an .xlsx attachment is intercepted
# and converted server-side to a plain-text table BEFORE it ever reaches a
# provider (see app/spreadsheet_ingestion.py) — providers.py never sees the
# spreadsheetml mime. A bare http(s) URL is rejected for the same SSRF
# reason as images (see _DATA_IMAGE_URL_RE). A .csv attachment needs no
# entry here at all: the frontend normalizes its mime to text/plain before
# it's ever sent (same treatment an unrecognized .md mime already gets),
# so it's indistinguishable from a .txt file by the time it arrives.
_MAX_INPUT_FILES = 4
_MAX_INPUT_FILE_CHARS = 20_000_000
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DATA_FILE_URL_RE = re.compile(
    r"^data:(application/pdf|text/plain|"
    r"application/vnd\.openxmlformats-officedocument\.spreadsheetml\.sheet)"
    r";base64,[A-Za-z0-9+/]+=*$"
)


class FileAttachment(BaseModel):
    """A document (PDF, plain text, or .xlsx) the user attached for the
    model to read — see _DATA_FILE_URL_RE above for why .xlsx is accepted
    here at all despite no provider understanding it natively."""

    filename: str = Field(..., min_length=1, max_length=200)
    data: str = Field(..., description="data:{application/pdf,text/plain};base64,...")

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("filename must not be empty")
        return cleaned

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: str) -> str:
        if len(value) > _MAX_INPUT_FILE_CHARS:
            raise ValueError("attached file is too large")
        if not _DATA_FILE_URL_RE.match(value):
            raise ValueError(
                "files must be data:{application/pdf,text/plain};base64,... URLs"
            )
        return value


# Audio attachments (meeting/voice-memo ingestion, distinct from the mic-
# button dictation flow that hits /v1/transcribe directly): at most this many
# per message, well below _MAX_INPUT_FILES since a single attached clip's
# transcript will itself become one of those file attachments. Sized to
# OpenAI's transcription API's real 25MB request limit (not a soft app-level
# truncation) -- v1 scope decision: a clip that doesn't fit is rejected with
# a clear message rather than chunked/split, since chunking would need
# client-side audio decoding this app has no other reason to carry.
_MAX_INPUT_AUDIO = 2
_MAX_INPUT_AUDIO_CHARS = 33_500_000  # ~25MB raw at the 4/3 base64 overhead
# Kept in sync with transcription._SUPPORTED_AUDIO_MIMES by hand (schemas.py
# stays a leaf module with no import of the transcription/orchestrator stack).
_DATA_AUDIO_URL_RE = re.compile(
    r"^data:audio/(webm|wav|mpeg|mp3|mp4|m4a|ogg);base64,[A-Za-z0-9+/]+=*$"
)


class AudioAttachment(BaseModel):
    """A recorded/uploaded audio clip (meeting recording, voice memo) the
    user attached for server-side transcription -- see app/audio_ingestion.py.
    The audio bytes themselves are never persisted; only the transcript (as a
    FileAttachment) and this clip's {filename, duration_seconds} survive past
    the request that attached it."""

    filename: str = Field(..., min_length=1, max_length=200)
    data: str = Field(
        ..., description="data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};base64,..."
    )
    # Client-measured playback length, for the UI's audio chip -- this app
    # doesn't decode audio server-side to compute it independently, same
    # trust level as any other client-reported display metadata (e.g. a
    # filename).
    duration_seconds: float | None = Field(default=None, ge=0)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("filename must not be empty")
        return cleaned

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: str) -> str:
        if len(value) > _MAX_INPUT_AUDIO_CHARS:
            raise ValueError(
                "attached audio exceeds the 25MB transcription limit "
                "(split it into a shorter clip)"
            )
        if not _DATA_AUDIO_URL_RE.match(value):
            raise ValueError(
                "audio must be a data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};"
                "base64,... URL"
            )
        return value


class AudioMeta(BaseModel):
    """Persisted, UI-facing metadata for one transcribed audio clip -- see
    AudioAttachment. Never carries the audio itself."""

    filename: str
    duration_seconds: float | None = None


def _validate_audio_list(
    value: list[AudioAttachment] | None,
) -> list[AudioAttachment] | None:
    if not value:
        return None
    if len(value) > _MAX_INPUT_AUDIO:
        raise ValueError(f"at most {_MAX_INPUT_AUDIO} audio clips per message")
    return value


def _validate_image_list(value: list[str] | None) -> list[str] | None:
    """Shared by AskRequest.images and ImportMessage.images — same caps,
    same data: URL requirement, so an imported attachment can't bypass any
    check a freshly-attached one would have to pass."""
    if not value:
        return None
    if len(value) > _MAX_INPUT_IMAGES:
        raise ValueError(f"at most {_MAX_INPUT_IMAGES} images per message")
    for url in value:
        if len(url) > _MAX_INPUT_IMAGE_CHARS:
            raise ValueError("attached image is too large")
        if not _DATA_IMAGE_URL_RE.match(url):
            raise ValueError(
                "images must be data:image/{png,jpeg,gif,webp};base64,... URLs"
            )
    return value


def _validate_video_list(value: list[str] | None) -> list[str] | None:
    """Used by ImportMessage.videos (and MessageRestoreRequest through it), so
    a video arriving back through Import/Undo has to pass the same shape check
    the generator's own output does — an imported clip can't bypass what a
    freshly-generated one satisfies by construction."""
    if not value:
        return None
    if len(value) > _MAX_VIDEOS:
        raise ValueError(f"at most {_MAX_VIDEOS} video per message")
    for url in value:
        if len(url) > _MAX_VIDEO_CHARS:
            raise ValueError("video is too large")
        if not _DATA_VIDEO_URL_RE.match(url):
            raise ValueError("videos must be data:video/mp4;base64,... URLs")
    return value


def _validate_file_list(
    value: list[FileAttachment] | None,
) -> list[FileAttachment] | None:
    """Shared by AskRequest.files and ImportMessage.files — FileAttachment's
    own field_validator already caps a single file's size/mime; this only
    adds the per-message count cap."""
    if not value:
        return None
    if len(value) > _MAX_INPUT_FILES:
        raise ValueError(f"at most {_MAX_INPUT_FILES} files per message")
    return value


# A generous cap, not a meaningful constraint on ordinary use (pasting a long
# document to ask about is fine) — a defensive limit against an unbounded
# body, consistent with every other free-text field in this schema module
# (e.g. imported messages at _MAX_IMPORT_MESSAGE_CHARS below).
_MAX_QUESTION_CHARS = 100_000

# A client-generated idempotency key (see app/request_registry.py) — a UUID
# is 36 chars; capped well above that (not exactly 36) so a client using a
# slightly different unique-id scheme (ULID, nanoid, ...) still works, while
# still rejecting anything resembling a free-text field smuggled through
# this param.
_MAX_REQUEST_ID_CHARS = 128


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=_MAX_QUESTION_CHARS,
        description="User question/prompt",
    )
    mode: Mode = Field(default=Mode.auto, description="Routing mode")
    no_cache: bool = Field(
        default=False,
        description="Bypass the response cache entirely — no read and no write",
    )
    model: str | None = Field(
        default=None,
        description="Force this exact model, bypassing routing (also skips cache)",
    )
    images: list[str] | None = Field(
        default=None,
        description=(
            "Attached vision input, as data:image/...;base64,... URLs "
            f"(max {_MAX_INPUT_IMAGES})"
        ),
    )
    files: list[FileAttachment] | None = Field(
        default=None,
        description=f"Attached documents (PDF or plain text), max {_MAX_INPUT_FILES}",
    )
    audio: list[AudioAttachment] | None = Field(
        default=None,
        description=(
            "Attached audio clips (meeting recording, voice memo) for "
            f"server-side transcription, max {_MAX_INPUT_AUDIO}. Each clip's "
            "transcript is folded into `files` as a plain-text document "
            "attachment before the model ever sees it -- see "
            "app/audio_ingestion.py."
        ),
    )
    research: bool = Field(
        default=False,
        description=(
            "Force a live web search for this question, regardless of the "
            "auto-mode classifier's freshness judgment. No-op if WEB_SEARCH "
            "isn't enabled or the resolved model isn't OpenAI-served."
        ),
    )
    request_id: str | None = Field(
        default=None,
        max_length=_MAX_REQUEST_ID_CHARS,
        description=(
            "Client-generated idempotency key (a UUID). A duplicate arrival "
            "of the same request_id (double-click, a client-side retry) is "
            "joined to the original call's in-flight or already-finished "
            "result instead of dispatching a second paid model call. "
            "Optional — omit for the old, untracked behavior."
        ),
    )

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        return _clean_forced_model(value)

    @field_validator("images")
    @classmethod
    def _validate_images(cls, value: list[str] | None) -> list[str] | None:
        return _validate_image_list(value)

    @field_validator("files")
    @classmethod
    def _validate_files(
        cls, value: list[FileAttachment] | None
    ) -> list[FileAttachment] | None:
        return _validate_file_list(value)

    @field_validator("audio")
    @classmethod
    def _validate_audio(
        cls, value: list[AudioAttachment] | None
    ) -> list[AudioAttachment] | None:
        return _validate_audio_list(value)


class Source(BaseModel):
    """A web citation the model's answer relied on (web_search retrieval)."""

    title: str
    url: str


class PendingAction(BaseModel):
    """A real-world action the model proposed (send email, update a sheet, ...).

    Propose-then-confirm: this is only ever a PROPOSAL. Nothing fires until the
    caller explicitly confirms it via POST .../messages/{id}/action — the model
    can suggest an action but never execute one unilaterally.
    """

    action: str
    summary: str
    payload: dict[str, object]


# Non-image files a sandboxed code_execution/code_interpreter run produced
# (a saved .xlsx/.csv/.docx/.pdf, ...) -- capped the same way FileAttachment's
# INPUT documents are, even though this is server-generated rather than user
# input: it still ends up stored in the DB and downloaded by the browser like
# any other attachment, so an unbounded or arbitrary-mime sandbox output
# shouldn't get a free pass just because a model produced it instead of a user.
_MAX_CODE_FILE_CHARS = 14_000_000  # ~10MB raw at the 4/3 base64 overhead
_CODE_FILE_MIME_ALLOWLIST = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/pdf",
    "text/csv",
    "application/json",
    "text/plain",
}

# A fixed filename-extension -> mime-type map, deliberately NOT
# `mimetypes.guess_type` (stdlib): that function augments its built-in table
# from the OS's own registry on Windows, which can disagree with the IANA
# standard for a common type -- e.g. a stock Windows install maps `.csv` to
# `application/vnd.ms-excel`, not `text/csv`, silently failing the exact
# allowlist check this map exists to pass. Both providers.py's Anthropic
# Files-API download (as a fallback when the API's own reported mime type is
# generic/unhelpful) and orchestrator_extract.py's OpenAI containers-API
# download (as the only signal available, since a container_file_citation
# carries no mime type of its own) key off this instead, so file-type
# detection for a generated file is deterministic across every OS this app
# runs on.
_CODE_FILE_EXTENSION_MIME_MAP: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    # A diagram drawn by code is very often an SVG (see routing.md: a
    # structural drawing is routed to code execution precisely because
    # vector output keeps its labels legible). Without this entry the
    # extension is unrecognised, so guess_code_file_mime returns None and
    # the file is dropped as "unsupported file type" — the one output the
    # diagram path is most likely to produce, discarded on arrival.
    ".svg": "image/svg+xml",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
}


def guess_code_file_mime(filename: str) -> str | None:
    """The mime type this app associates with a generated file's extension,
    or None if unrecognized -- see _CODE_FILE_EXTENSION_MIME_MAP above for
    why this exists instead of stdlib `mimetypes.guess_type`."""
    ext = filename[filename.rfind(".") :].lower() if "." in filename else ""
    return _CODE_FILE_EXTENSION_MIME_MAP.get(ext)


def dedupe_code_files(results: list[dict[str, object]]) -> None:
    """Drop repeats of the same generated file ACROSS a call's code results,
    in place, keeping the last occurrence of each filename.

    A model that produces a file rarely stops there: it re-reads it to check
    the row count, or rewrites it after spotting a gap. The sandbox container
    still holds the file, so every one of those runs reports it again and each
    is downloaded and attached to its own result. Observed live: one
    12,922-byte .xlsx returned twice from a three-run answer, which reaches
    the user as two identical download links, and is stored and re-sent at
    twice the size.

    Keyed on the FILENAME, and keeping the LAST, which handles both shapes
    with one rule. Re-read unchanged: the copies are identical, so which one
    survives cannot matter. Rewritten: the later version is the corrected one,
    and showing the superseded copy beside it under the same name would be
    worse than showing neither.

    Deliberately not keyed on the file's bytes: two files with the same name
    and different contents are one file at two moments, not two deliverables,
    and offering both invites downloading the wrong one.

    Images are untouched — they render inline rather than as downloads, and a
    repeated chart is a visible duplicate a reader can simply scroll past,
    not a fork in which file is the real one.
    """

    def files_of(result: dict[str, object]) -> list[dict[str, object]]:
        """The result's file list, or [] for the "no files" and
        never-populated shapes — narrowed rather than asserted, since these
        dicts are assembled from provider responses whose shape this module
        does not control."""
        files = result.get("files")
        return files if isinstance(files, list) else []

    def name_of(file: object) -> str:
        return str(file.get("filename", "")) if isinstance(file, dict) else ""

    # Position is (which result, where in its list), not just the result: the
    # OpenAI path collects every container_file_citation across the whole
    # response into ONE list, so its repeats sit side by side rather than in
    # separate results. Keying on the result alone let those both survive.
    last_seen: dict[str, tuple[int, int]] = {}
    for position, result in enumerate(results):
        for index, file in enumerate(files_of(result)):
            name = name_of(file)
            if name:
                last_seen[name] = (position, index)

    for position, result in enumerate(results):
        files = files_of(result)
        if not files:
            continue
        result["files"] = [
            file
            for index, file in enumerate(files)
            # An entry with no readable filename is passed through untouched:
            # these come from provider responses whose shape this app does not
            # control, and a dropped deliverable is much the worse failure.
            if not name_of(file) or last_seen.get(name_of(file)) == (position, index)
        ]


class CodeFile(BaseModel):
    """A non-image file a code_execution/code_interpreter sandbox run
    produced -- see app/orchestrator_extract.py and app/providers.py for how
    these are downloaded from the provider's container/Files API. Images from
    the same run stay in CodeResult.images unchanged, since they're rendered
    inline rather than offered as a download."""

    filename: str = Field(..., min_length=1, max_length=200)
    mime_type: str
    data: str = Field(..., description="data:<mime_type>;base64,...")

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("filename must not be empty")
        return cleaned

    @field_validator("mime_type")
    @classmethod
    def _validate_mime_type(cls, value: str) -> str:
        if value not in _CODE_FILE_MIME_ALLOWLIST:
            raise ValueError(f"unsupported code-execution file type: {value}")
        return value

    @field_validator("data")
    @classmethod
    def _validate_data(cls, value: str) -> str:
        if len(value) > _MAX_CODE_FILE_CHARS:
            raise ValueError("code-execution file is too large")
        if not value.startswith("data:") or ";base64," not in value:
            raise ValueError("data must be a data:<mime_type>;base64,... URL")
        return value


class CodeResult(BaseModel):
    """One code_interpreter tool call: the Python the model ran, in OpenAI's own
    sandboxed container (never on this machine), plus whatever it produced."""

    code: str
    logs: str | None = None
    # data:image/...;base64,... URLs, if the code produced a plot/chart.
    images: list[str] | None = None
    # Non-image outputs (a generated .xlsx/.csv/.docx/.pdf, ...), if any.
    files: list[CodeFile] | None = None
    # One human-readable line per generated file that the sandbox reported
    # but this app could NOT attach (an unsupported/undetected mime type, an
    # oversized file, or a failed download) — a visible replacement for the
    # silent drop this used to be. Never populated just because a run
    # produced zero files; only when a file was reported and then lost.
    file_warnings: list[str] | None = None


class SpreadsheetPreviewRequest(BaseModel):
    """POST /v1/spreadsheet-preview's body — the SAME CodeFile the message
    already carries (filename + data URI), re-sent so the backend can parse
    it into a small preview grid on demand rather than the frontend bundling
    a spreadsheet-parsing dependency. Only ever a generated .xlsx/.csv file
    the caller already received in a code_results entry; this endpoint holds
    no state of its own."""

    filename: str = Field(..., min_length=1, max_length=200)
    data: str = Field(..., description="data:<mime_type>;base64,...")


class SpreadsheetPreviewResponse(BaseModel):
    """First ~50 rows x ~20 columns of a generated tabular file, for an
    inline UI preview — see app/spreadsheet_ingestion.py's
    xlsx_preview_rows/csv_preview_rows. `total_rows`/`total_cols` are the
    file's REAL dimensions, so the UI can state the shape of the whole file
    and say outright when the grid it's showing is only part of it;
    `truncated` is true when they exceed what's actually in `rows`.
    `sheet_name` is the worksheet's own title for an .xlsx and None for a
    .csv (no sheets), which the UI renders as the filename instead."""

    rows: list[list[str]]
    total_rows: int
    total_cols: int
    truncated: bool
    sheet_name: str | None = None


class LibrarySource(BaseModel):
    """One document from the owner's RAG document library whose chunks were
    recalled into an answer's context (see app/rag_library.py) — a summary
    for the UI/audit trail, not the raw chunk text itself."""

    document: str
    snippet_count: int


class MemorySource(BaseModel):
    """One past exchange from the owner's OTHER conversations recalled into
    this answer's context (see app/memory.py) — provenance only (which
    conversation, when), never the recalled question/answer text itself
    (that's already folded into the prompt as context). The user-facing
    defense against memory's entity-swap false-positive risk: showing
    exactly what was recalled lets the caller judge a mismatch the
    similarity threshold couldn't catch."""

    conversation_title: str
    created_at: str


class WorkflowStep(BaseModel):
    """One step of an opt-in multi-step workflow plan (mode="workflow"; see
    app/workflow.py) — a per-step breakdown for the UI/audit trail, mirroring
    LibrarySource's role for the RAG library. `status` is "ok" or "failed":
    a failed step still appears here (with whatever partial info is known)
    rather than being silently dropped, so the breakdown always accounts for
    every planned step."""

    category: str
    instruction: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    status: str


class LibraryDocument(BaseModel):
    """One uploaded document in the owner's RAG library (GET/POST
    /v1/library/documents) — metadata only, never the extracted text/chunks
    themselves (those exist only server-side, for retrieval)."""

    id: int
    filename: str
    mime_type: str
    size_bytes: int
    chunk_count: int
    created_at: str


class FactCheck(BaseModel):
    """One published fact-check relevant to a claim the user asked to
    verify, from Google's Fact Check Tools API (see app/fact_check.py)."""

    claim: str
    rating: str | None = None
    publisher: str | None = None
    url: str | None = None


class AcademicResult(BaseModel):
    """One scholarly work relevant to a research-literature question, from
    OpenAlex (see app/academic_search.py)."""

    title: str
    authors: str | None = None
    year: int | None = None
    venue: str | None = None
    citation_count: int | None = None
    url: str | None = None
    abstract_snippet: str | None = None


class MathResult(BaseModel):
    """One math_solve tool call: an exact symbolic/numeric result computed
    by SymPy (see app/math_solve.py), or the reason it couldn't be."""

    operation: str
    expression: str
    variable: str
    result: str | None = None
    error: str | None = None
    # "sympy" or "wolfram_alpha" — which engine actually produced `result`.
    # None when there's no result (error case) or for entries persisted
    # before this field existed.
    source: str | None = None


def _current_deployment_id() -> str:
    """Lazy import: schemas is a near-leaf module many things import — see
    self_describe.py's cycle notes — and the factory only runs at response
    CONSTRUCTION time, when app.database is long since loaded."""
    from . import database

    return database.deployment_id()


class AskResponse(BaseModel):
    answer: str
    mode_used: str
    notes: str
    # The database identity this answer was computed against, stamped by
    # default_factory so EVERY construction site — primary, fallback, both
    # cache hits, and any future one — carries it without remembering to.
    # Fresh at serve time even for a cached answer (the reconstruction
    # builds a new AskResponse): the point is provenance of the RESPONSE,
    # not of the stored entry. The frontend warns when it changes
    # mid-session — a different deployment answering the port (see
    # database.deployment_id for the incident).
    deployment_id: str = Field(default_factory=_current_deployment_id)
    # A plain-English counterpart to the technical diagnostic already in
    # `notes` (exception type, request_id, elapsed ms — unchanged, still
    # what's logged/shown in a details disclosure). None for a normal
    # successful answer; the client shows THIS as the primary failure
    # headline instead of `notes` verbatim. See
    # orchestrator._fallback_exhausted_failure_message and its call sites.
    #
    # Usually set alongside an EMPTY `answer` (a failed request — budget
    # refusal, or every model/fallback candidate failing outright), but not
    # only: a workflow that had to stop one step and still delivered the rest
    # sets this next to a real answer (see workflow._missing_input_failure_
    # message). The client already handles both — the headline is shown
    # either way, and only an empty answer additionally turns it red and
    # raises the "didn't get an answer" notice.
    failure_message: str | None = None
    # The exact model that answered — for a workflow, the SYNTHESIS step's
    # model; for a cache hit, whichever model answered the original call.
    # None for a response with no real model call behind it: an ambiguous
    # clarifying question, a moderation refusal, a no-api-key or
    # budget-refusal note.
    #
    # This comment used to claim "every other path sets it", and that claim is
    # why an empty badge went unnoticed for so long: `mode="workflow"` set the
    # field on its AskResponse and then dropped it in the router's own
    # response builder, so the documented invariant read as satisfied while
    # the wire format said otherwise. It is now carried by a single shared
    # builder (routers/messages/ask.py's _api_response), which is what makes
    # the statement true rather than aspirational — do not restore the old
    # wording without checking the builders.
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None
    # The actual search-query text the web_search tool issued, distinct from
    # `sources` (the RESULTS a search returned) — see
    # orchestrator_extract._extract_search_queries/
    # providers._extract_anthropic_search_queries.
    search_queries: list[str] | None = None
    pending_action: PendingAction | None = None
    # Generated images (image_generation tool), as ready-to-render
    # `data:image/png;base64,...` URLs.
    images: list[str] | None = None
    # Generated video (see app/video_generation.py), as ready-to-render
    # `data:video/mp4;base64,...` URLs. A list for shape-parity with `images`
    # all the way through persistence and the UI. An ordinary ask puts at most
    # ONE entry here — at this price, generating several per turn would be a
    # footgun — but a workflow can contribute one per step (see _MAX_VIDEOS).
    videos: list[str] | None = None
    # Code the model ran via the code_interpreter tool, in order.
    code_results: list[CodeResult] | None = None
    # Published fact-checks surfaced for a claim-verification question.
    fact_checks: list[FactCheck] | None = None
    # Scholarly works surfaced for a research-literature question.
    academic_results: list[AcademicResult] | None = None
    # Exact symbolic/numeric results computed via the math_solve tool.
    math_results: list[MathResult] | None = None
    # Documents from the owner's RAG library whose chunks were recalled into
    # this answer's context (see app/rag_library.py). None when RAG_LIBRARY
    # is off or nothing in the library cleared the similarity bar.
    library_sources: list[LibrarySource] | None = None
    # Past exchanges from the owner's OTHER conversations recalled into this
    # answer's context (see app/memory.py). None when CROSS_CONVERSATION_MEMORY
    # is off or nothing recalled cleared the similarity bar.
    memory_sources: list[MemorySource] | None = None
    # Per-step breakdown for an opt-in multi-step workflow answer (see
    # app/workflow.py); None for every ordinary (non-workflow) answer.
    workflow_steps: list[WorkflowStep] | None = None
    # True when the provider stopped generating because it hit
    # max_output_tokens, not because it was actually finished — the answer is
    # genuinely incomplete, not just short. The UI offers a Continue action.
    truncated: bool = False
    # The output-token ceiling this answer was generated under — the tier's
    # budget from RouteDecision.max_output_tokens (see routing.tier_output_caps).
    # Carried on the response, and persisted with the message, so a truncated
    # answer can name the limit it actually hit rather than the limit today's
    # configuration would impose, and so the re-route control can tell which of
    # its options have more headroom than the attempt that just failed. None
    # for a workflow answer, which has no single ceiling (each step has its
    # own), and for anything persisted before the column existed.
    max_output_tokens: int | None = None
    # True when the call hit `max_output_tokens` before emitting ANY text of
    # its own — the whole ceiling went on a tool call's arguments or private
    # reasoning — so `answer` is the app's explanation, not a partial answer.
    # Always accompanies `truncated`; the two differ in what they license.
    # `truncated` alone means "resume this" (Continue); this one means there
    # is nothing to resume, so Continue is refused for such a message and the
    # remedy is a re-run with more headroom ("Retry as workflow").
    no_output: bool = False
    # Whether this answer may be written to cross-conversation memory (see
    # app/memory.py's remember). False when the app appended its own
    # capabilities snapshot to the answer text — the effective model map,
    # enabled feature flags, request limits, free-lane quotas, and the
    # owner's REMAINING DAILY BUDGET IN USD (see self_describe.format_note).
    # The response cache already refuses to store exactly this
    # (orchestrator.py's `cacheable_answer`, `and not capabilities_calls`)
    # because it reflects live per-owner state; memory had no equivalent
    # guard and, unlike the cache, no TTL — app/retention.py never prunes it,
    # so an account snapshot written there persists until 500 newer entries
    # evict it.
    #
    # `exclude=True`: this is a routing signal from the orchestrator to the
    # ask route (the only layer that calls memory.remember), not something a
    # client has any use for — it stays off the wire and out of the OpenAPI
    # schema. model_copy still carries it, so _api_response cannot drop it.
    memorable: bool = Field(default=True, exclude=True)


_MIN_COMPARE_MODELS = 2
_MAX_COMPARE_MODELS = 4


class CompareRequest(BaseModel):
    """Ask the same question of several specific models side-by-side — a
    direct, one-shot comparison tool distinct from routing (nothing here is
    persisted as a conversation)."""

    question: str = Field(..., min_length=1, max_length=_MAX_QUESTION_CHARS)
    models: list[str] = Field(
        ..., min_length=_MIN_COMPARE_MODELS, max_length=_MAX_COMPARE_MODELS
    )

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        cleaned = [validate_model_value(model) for model in value]
        if any(not model for model in cleaned):
            raise ValueError("model names must not be empty")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("models must be distinct")
        return cleaned


class CompareResult(BaseModel):
    model: str
    answer: str
    mode_used: str
    notes: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    elapsed_ms: int


class CompareResponse(BaseModel):
    question: str
    results: list[CompareResult]


_MIN_CHAT_MESSAGES = 1
_MAX_CHAT_MESSAGES = 100


class ChatMessage(BaseModel):
    """One message in an OpenAI-shaped chat/completions request or response."""

    role: Literal["system", "user", "assistant"]
    content: str = Field(..., max_length=_MAX_QUESTION_CHARS)


class ChatCompletionRequest(BaseModel):
    """OpenAI /v1/chat/completions-compatible request — lets any tool built
    against the OpenAI SDK/wire format point at this app and inherit its
    routing, caching, and budget behavior instead of talking to OpenAI
    directly. Only the fields this app can act on are declared; the rest of
    a typical client's payload (temperature, top_p, presence_penalty, n,
    stop, ...) is accepted and silently ignored (Pydantic's default
    extra="ignore") rather than rejected — this app's mode/routing already
    determines sampling behavior, there's nothing for those knobs to control.

    Unlike the stateful /v1/conversations/{id}/ask endpoints, this is
    STATELESS like /v1/ask: nothing is persisted, and the full conversation
    must be resent in `messages` every call, exactly like the real OpenAI API.
    """

    model: str = Field(
        default="auto",
        description=(
            "A routing mode (auto/budget/fast/smart), or any other value to "
            "force that exact model (bypassing routing and the cache, like "
            "AskRequest.model)"
        ),
    )
    messages: list[ChatMessage] = Field(
        ..., min_length=_MIN_CHAT_MESSAGES, max_length=_MAX_CHAT_MESSAGES
    )
    stream: bool = False

    @field_validator("messages")
    @classmethod
    def _validate_messages(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        if value[-1].role != "user":
            raise ValueError("the last message must have role 'user'")
        return value


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Literal["stop", "length"]


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage


class EstimateRequest(BaseModel):
    """A composer-preview request: what would this question cost if sent,
    without actually sending it. Same size cap as AskRequest.question, since
    this is meant to be called with the exact text the user is about to ask."""

    question: str = Field(..., min_length=1, max_length=_MAX_QUESTION_CHARS)
    mode: Mode = Field(default=Mode.auto, description="Routing mode")


class EstimateResponse(BaseModel):
    """`cost_usd_estimate` is the whole worst case, artefacts included.

    `video_cost_usd_estimate` and `image_cost_usd_estimate` break out how much
    of it is a generated artefact, and are None whenever none is projected — so
    the UI can say WHY the figure moved instead of showing an unexplained jump
    that reads like a bug. Both are COMPONENTS of the total, never additions to
    it: summing them with cost_usd_estimate would double-count. They can be set
    together, because dispatch reserves for both when both are in play.

    `image_is_certain` distinguishes the two ways an image cost arises, which
    the money alone cannot. False means the hosted OpenAI tool is merely being
    OFFERED — true for ANY question under an OpenAI model with that backend,
    including one that plainly wants no picture, since the model decides for
    itself whether to call it. True means this app will make the image call
    directly because the question reads as asking for one. Both are reserved
    against identically, so the estimate must count them identically; only the
    wording the UI chooses should differ, or a preview that says "includes
    $0.19 for an image" on "what is the capital of France" trains people to
    stop believing it.
    """

    model: str
    mode_used: str
    input_tokens_estimate: int
    output_tokens_estimate: int
    cost_usd_estimate: float | None = None
    video_cost_usd_estimate: float | None = None
    image_cost_usd_estimate: float | None = None
    image_is_certain: bool = False


class ModelCatalogStatus(BaseModel):
    """See app/model_catalog.py. `synced_at` is null when a sync has never
    completed; `new_models` lists model names first seen in the most recent
    sync (empty on the very first sync — nothing to diff against yet).
    `error` is only present when a sync was just attempted and failed (the
    previously-cached catalog, if any, is left untouched)."""

    enabled: bool
    synced_at: str | None = None
    model_count: int = 0
    new_models: list[str] = Field(default_factory=list)
    stale: bool = False
    error: str | None = None


class RegenerateRequest(BaseModel):
    """Re-run the conversation's last user question (always fresh, no cache)."""

    mode: Mode = Field(default=Mode.auto, description="Routing mode for the retry")
    model: str | None = Field(
        default=None, description="Force this exact model for the regeneration"
    )
    request_id: str | None = Field(
        default=None,
        max_length=_MAX_REQUEST_ID_CHARS,
        description="Client-generated idempotency key — see AskRequest.request_id.",
    )

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str | None) -> str | None:
        return _clean_forced_model(value)


class ConversationCreate(BaseModel):
    title: str = Field(default="Untitled conversation", min_length=1)


class ConversationUpdate(BaseModel):
    title: str = Field(..., min_length=1)


# A previously exported conversation is re-created from scratch (fresh ids,
# no model calls) rather than restoring the original row-for-row. Everything
# duplicate_conversation() also copies (pin, instructions, favorite status,
# and per-message tokens/cost/cached/sources) is restored here too, for the
# same reason Export produces it in the first place: text, numbers, and
# title/url pairs. Attachments (images/files) round-trip too, but through
# the SAME validators AskRequest.images/files apply to a freshly-attached
# upload (_validate_image_list/_validate_file_list below, defined earlier
# in this module) — a re-imported attachment gets no less scrutiny than one
# just attached in the UI: same count cap, same size cap, same data: URL /
# mime allowlist. An oversized or malformed attachment in an otherwise-valid
# export fails the whole import (422) rather than silently dropping just
# that attachment, so a stale/tampered export file can't sneak one past.
_MAX_IMPORT_MESSAGES = 500
_MAX_IMPORT_MESSAGE_CHARS = 100_000
_MAX_SYSTEM_PROMPT_CHARS = 4_000


class ImportMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=_MAX_IMPORT_MESSAGE_CHARS)
    mode_used: str | None = None
    notes: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None
    search_queries: list[str] | None = None
    truncated: bool = False
    # See Message.max_output_tokens. Round-trips so a re-imported or restored
    # truncated answer still names its own ceiling; `ge=0` because a negative
    # ceiling describes no attempt that ever happened.
    max_output_tokens: int | None = Field(default=None, ge=0)
    code_results: list[CodeResult] | None = None
    fact_checks: list[FactCheck] | None = None
    academic_results: list[AcademicResult] | None = None
    math_results: list[MathResult] | None = None
    library_sources: list[LibrarySource] | None = None
    memory_sources: list[MemorySource] | None = None
    workflow_steps: list[WorkflowStep] | None = None
    # The literal model that answered (see AskResponse.model); None for a
    # user message or a message persisted before this field existed.
    model: str | None = None
    # This caller's 👍/👎 (1/-1) on an assistant message, or None if never
    # rated/cleared — see MessageFeedback below. Carried through duplicate/
    # export/import for field parity, unlike `bookmarked` (see
    # database.py's add_message docstring).
    feedback: int | None = None
    feedback_reason: str | None = Field(default=None, max_length=200)
    images: list[str] | None = Field(
        default=None,
        description=(
            "Vision input attached to this message, as "
            f"data:image/...;base64,... URLs (max {_MAX_INPUT_IMAGES})"
        ),
    )
    # Generated video, unlike every other field here: carried through
    # Export/Import/Restore so an Undo of a deleted answer, or a re-imported
    # conversation, brings the clip back rather than the prose that refers to
    # one. Dropping it would lose the single most expensive artefact in a
    # conversation, silently.
    videos: list[str] | None = Field(
        default=None,
        description="Generated video for this message, as a data:video/mp4;base64,... URL",
    )
    files: list[FileAttachment] | None = Field(
        default=None,
        description=(
            f"Documents (PDF or plain text) attached to this message, max {_MAX_INPUT_FILES}"
        ),
    )
    # Metadata only (no audio bytes to round-trip — see AudioMeta); carried
    # through Export/Import/Duplicate/Branch for the same field parity as
    # every other attachment type.
    audio: list[AudioMeta] | None = None

    @field_validator("images")
    @classmethod
    def _validate_images(cls, value: list[str] | None) -> list[str] | None:
        return _validate_image_list(value)

    @field_validator("videos")
    @classmethod
    def _validate_videos(cls, value: list[str] | None) -> list[str] | None:
        return _validate_video_list(value)

    @field_validator("files")
    @classmethod
    def _validate_files(
        cls, value: list[FileAttachment] | None
    ) -> list[FileAttachment] | None:
        return _validate_file_list(value)

    @field_validator("audio")
    @classmethod
    def _validate_audio_meta(
        cls, value: list[AudioMeta] | None
    ) -> list[AudioMeta] | None:
        if not value:
            return None
        if len(value) > _MAX_INPUT_AUDIO:
            raise ValueError(f"at most {_MAX_INPUT_AUDIO} audio clips per message")
        return value

    @field_validator("feedback")
    @classmethod
    def _validate_feedback(cls, value: int | None) -> int | None:
        if value is not None and value not in (1, -1):
            raise ValueError("feedback must be 1, -1, or null")
        return value


_MAX_TAGS = 15
_MAX_TAG_LENGTH = 30


def _normalize_tags(tags: list[str]) -> list[str]:
    """Trim, drop blanks/dupes, cap length — used by both the tags-set
    endpoint and Import, so the two accept the same shape."""
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags:
        cleaned = tag.strip()[:_MAX_TAG_LENGTH]
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


class ConversationImport(BaseModel):
    title: str = Field(default="Imported conversation", min_length=1, max_length=200)
    pinned_model: str | None = Field(default=None, max_length=200)
    system_prompt: str | None = Field(default=None, max_length=_MAX_SYSTEM_PROMPT_CHARS)
    favorite: bool = False
    tags: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)
    messages: list[ImportMessage] = Field(
        ..., min_length=1, max_length=_MAX_IMPORT_MESSAGES
    )

    @field_validator("pinned_model")
    @classmethod
    def _validate_pinned_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = validate_model_value(value)  # raises on a malformed name
        return cleaned or None

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)


class MessageRestoreRequest(ImportMessage):
    """Body for POST .../messages/restore — recreates one message (fresh id,
    no model call) in its existing conversation. The backing endpoint for
    Undo after deleting a message; same shape and same fidelity (including
    attachments) as a single ImportMessage."""


class ConversationOut(BaseModel):
    id: int
    title: str
    owner: str | None = None
    pinned_model: str | None = None
    system_prompt: str | None = None
    favorite: bool = False
    archived: bool = False
    tags: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str
    # Only populated by the list endpoint (a correlated subquery there); every
    # other conversation-returning endpoint omits it and this default stands
    # in, since none of them render a message count.
    message_count: int = 0

    @field_validator("tags", mode="before")
    @classmethod
    def _parse_tags(cls, value: object) -> object:
        # SQLite stores this as a JSON string; decode before validation.
        # Malformed JSON degrades to an empty list rather than a 500.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return []


class ConversationSystemPrompt(BaseModel):
    # Custom instructions (persona/style/rules) prepended to every question in
    # this conversation; empty string clears it.
    system_prompt: str = Field(default="", max_length=_MAX_SYSTEM_PROMPT_CHARS)


class ConversationFavorite(BaseModel):
    favorite: bool


class ConversationArchive(BaseModel):
    archived: bool


class ConversationTags(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=_MAX_TAGS)

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str]) -> list[str]:
        return _normalize_tags(value)


class ShareCreate(BaseModel):
    # Hours until the link expires; None (the default) means it never does.
    # Capped at 1 year so a forgotten link doesn't stay live forever, but
    # generous enough not to get in the way of any real use.
    ttl_hours: int | None = Field(default=None, ge=1, le=8760)


class ShareStatus(BaseModel):
    active: bool
    token: str | None = None
    expires_at: str | None = None


_MAX_TEMPLATE_NAME_CHARS = 80
_MAX_TEMPLATE_CONTENT_CHARS = 4_000


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=_MAX_TEMPLATE_NAME_CHARS)
    content: str = Field(..., min_length=1, max_length=_MAX_TEMPLATE_CONTENT_CHARS)


class TemplateUpdate(BaseModel):
    # Both optional so a rename doesn't require resending the content (and
    # vice versa); at least one must be given, enforced in the route.
    name: str | None = Field(
        default=None, min_length=1, max_length=_MAX_TEMPLATE_NAME_CHARS
    )
    content: str | None = Field(
        default=None, min_length=1, max_length=_MAX_TEMPLATE_CONTENT_CHARS
    )


class TemplateOut(BaseModel):
    id: int
    name: str
    content: str
    created_at: str
    updated_at: str


class MessageBookmark(BaseModel):
    bookmarked: bool


class MessageFeedback(BaseModel):
    """Body for PUT .../messages/{id}/feedback. `verdict=null` always
    clears; setting the SAME verdict that's already recorded also clears it
    (see database.set_message_feedback) — same click-again-to-clear UX
    contract as the bookmark toggle."""

    verdict: Literal["up", "down"] | None = None
    reason: str | None = Field(default=None, max_length=200)


class SearchResult(BaseModel):
    id: int
    title: str
    owner: str | None = None
    pinned_model: str | None = None
    created_at: str
    updated_at: str
    snippet: str


class UsageByModel(BaseModel):
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    # None when NONE of this model's calls in the window have a known cost
    # (an unpriced model — see usage.py's estimate_cost) — distinct from a
    # genuinely free model (e.g. local Ollama), which reports 0.0.
    cost_usd: float | None


class UsageByDay(BaseModel):
    date: str
    cost_usd: float
    # input_tokens + output_tokens billed that day (across every model) — the
    # other half of the tokens-per-dollar KPI (see UsageSummary.tokens_per_dollar).
    tokens: int = 0


class CachePerformance(BaseModel):
    """Cache effectiveness over the usage window — see app/cache_stats.py for
    what `total_requests` counts and why it is not simply the number of
    billed calls."""

    total_requests: int = 0
    exact_hits: int = 0
    semantic_hits: int = 0
    exact_hit_rate: float | None = None
    semantic_hit_rate: float | None = None
    avoided_cost_usd: float = 0.0


class UsageSummary(BaseModel):
    today_usd: float
    days: int
    by_model: list[UsageByModel]
    by_day: list[UsageByDay]
    # The configured caps themselves (not live global spend — that stays
    # private to the operator; see budget.py). None when that cap isn't set.
    daily_budget_usd: float | None = None
    daily_budget_per_owner_usd: float | None = None
    # How much of the caller's OWN per-owner cap is left today, floored at 0.
    # None when no per-owner cap is configured — distinct from "$0 left".
    owner_remaining_usd: float | None = None
    # This owner's own avoided cost today (see database.avoided_cost_log) —
    # spend the app's response cache prevented, never counted in `today_usd`
    # or against any budget cap, since no call actually happened.
    avoided_cost_today_usd: float = 0.0
    # The KPI this app actually exists to move: total tokens processed per
    # dollar spent over the window (sum of by_day.tokens / sum of
    # by_day.cost_usd) — routing/caching/downscaling work should show up here
    # as a rising number, not just as a shrinking spend figure that could
    # equally mean "used it less." None when the window has zero spend —
    # either no usage at all, or every call in it was free (see
    # window_tokens: distinguishes the two on the frontend).
    tokens_per_dollar: float | None = None
    # Total tokens processed over the window, regardless of cost — lets the
    # frontend tell "no usage" (0 tokens) apart from "all free" (tokens > 0,
    # tokens_per_dollar still None because cost was 0).
    window_tokens: int = 0
    # How much work the caches actually saved over the window (see
    # app/cache_stats.py), the same figures the weekly self-report prints.
    # Both rates are None when the window holds no requests at all, so the
    # frontend can show "—" rather than a 0% that would read as a cache
    # that is on but never hitting.
    cache: CachePerformance = Field(default_factory=lambda: CachePerformance())
    # The stable random identity of the database this response was computed
    # from (see database.deployment_id) — the frontend warns when it changes
    # mid-session, which means a DIFFERENT deployment answered the port.
    deployment_id: str = ""


class ConversationPin(BaseModel):
    # A model name (forced) or 'fast'/'smart' tier; empty string clears the pin.
    model: str = Field(default="", max_length=200)

    @field_validator("model")
    @classmethod
    def _validate_model(cls, value: str) -> str:
        return validate_model_value(value)  # raises on a malformed name; '' stays ''


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    mode_used: str | None = None
    notes: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    sources: list[Source] | None = None
    # See AskResponse.search_queries — same meaning, persisted with the message.
    search_queries: list[str] | None = None
    pending_action: PendingAction | None = None
    # "pending" | "confirmed" | "declined" | "failed"; None when there was never
    # a proposed action on this message.
    action_status: str | None = None
    # For an assistant message: images the model generated. For a user
    # message: images the user attached (vision input). Same shape either way
    # (data:image/...;base64,... URLs); `role` disambiguates the meaning.
    images: list[str] | None = None
    # Video the model generated, as a `data:video/mp4;base64,...` URL. Unlike
    # `images` above this is assistant-only: video is never an input here (a
    # clip the user attaches is transcribed to text — see app/audio_ingestion.py
    # for the audio equivalent), so `role` needs no disambiguating.
    videos: list[str] | None = None
    # Documents (PDF/plain text) the user attached; always None on assistant
    # messages — the model can read a file, never produce one.
    files: list[FileAttachment] | None = None
    # Audio clips the user attached, transcribed server-side — see
    # app/audio_ingestion.py. Metadata only (filename + duration); the
    # transcript itself lives in `files` like any other document attachment.
    # Always None on assistant messages.
    audio: list[AudioMeta] | None = None
    bookmarked: bool = False
    # See AskResponse.truncated — same meaning, persisted so it survives a
    # reload instead of only being known at the moment the answer streamed in.
    truncated: bool = False
    # See AskResponse.max_output_tokens — same meaning, persisted so a truncated
    # answer still names its own ceiling after a reload. Updated by a Continue
    # (append_to_message) to the continuation's ceiling, since that is the
    # attempt whose cut-off the notice is describing.
    max_output_tokens: int | None = None
    # See AskResponse.no_output — same meaning, persisted with the message.
    no_output: bool = False
    # See AskResponse.code_results — same meaning, persisted with the message.
    code_results: list[CodeResult] | None = None
    # See AskResponse.fact_checks — same meaning, persisted with the message.
    fact_checks: list[FactCheck] | None = None
    # See AskResponse.academic_results — same meaning, persisted with the message.
    academic_results: list[AcademicResult] | None = None
    # See AskResponse.math_results — same meaning, persisted with the message.
    math_results: list[MathResult] | None = None
    # See AskResponse.library_sources — same meaning, persisted with the message.
    library_sources: list[LibrarySource] | None = None
    # See AskResponse.memory_sources — same meaning, persisted with the message.
    memory_sources: list[MemorySource] | None = None
    # See AskResponse.workflow_steps — same meaning, persisted with the message.
    workflow_steps: list[WorkflowStep] | None = None
    # The literal model that answered (see AskResponse.model); None for a
    # user message or a message persisted before this column existed.
    model: str | None = None
    # This caller's own 👍/👎 (1/-1) on an assistant message, or None if
    # never rated/cleared (see app/feedback.py, MessageFeedback). A pure
    # marker like `bookmarked` — never affects the conversation's updated_at.
    feedback: int | None = None
    feedback_reason: str | None = None
    created_at: str

    @field_validator("cached", "truncated", mode="before")
    @classmethod
    def _coerce_cached(cls, value: object) -> bool:
        # SQLite stores this as 0/1/NULL; normalise to a bool for the API.
        return bool(value)

    @field_validator(
        "sources",
        "search_queries",
        "pending_action",
        "images",
        "videos",
        "files",
        "audio",
        "code_results",
        "fact_checks",
        "academic_results",
        "math_results",
        "library_sources",
        "memory_sources",
        "workflow_steps",
        mode="before",
    )
    @classmethod
    def _parse_json_column(cls, value: object) -> object:
        # SQLite stores these as a JSON string (or NULL); decode before pydantic
        # validates them. Malformed JSON degrades to None rather than a 500 — a
        # display nicety, not worth failing the request.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None


class BookmarkedMessage(MessageOut):
    """A bookmarked message plus its conversation's title, for a Bookmarks
    panel that lists all of a caller's bookmarks across every conversation
    without a separate per-conversation lookup."""

    conversation_title: str


class ConversationSpend(BaseModel):
    """What a conversation ACTUALLY cost, from the spend log rather than from
    its saved messages.

    A conversation's displayed total has always been summed from the messages
    it holds, which silently omits every call billed without producing one: a
    discarded regenerate, a cancelled stream, an answer that came back empty.
    One real session showed $0.1014 in the footer against $0.5742 billed.

    `cost_usd` is the true total; `unattributed_cost_usd` is the part of it
    with no message to hang off — i.e. exactly what the message-derived figure
    misses. Clients show the difference rather than quietly reconciling it,
    since "you were billed for answers you never received" is the fact worth
    surfacing, not a number to correct behind the scenes.
    """

    cost_usd: float
    input_tokens: int
    output_tokens: int
    unattributed_cost_usd: float


class SharedMessage(BaseModel):
    """One message as shown on a public read-only share link — deliberately
    a narrower view than MessageOut: no cost/token/model/notes fields (a
    share recipient shouldn't see the owner's spend or which model answered),
    no pending_action/action_status/bookmarked/feedback/feedback_reason
    (there's nothing an anonymous viewer could do with those anyway, and a
    feedback rating is this caller's own private signal, not something an
    anonymous recipient should see), and — unlike MessageOut — no
    library_sources: that field names documents from the owner's PRIVATE RAG
    library, which an anonymous share recipient has no business seeing even
    though the recalled snippet text itself is already folded into the
    answer content above. Same reasoning excludes memory_sources (it names
    the TITLES of the owner's other, unshared conversations) and
    workflow_steps: it's a per-step breakdown of which models answered
    which sub-instruction and what each step cost — exactly the "spend/
    which model answered" category already excluded above, just decomposed
    into steps instead of a single total."""

    role: str
    content: str
    created_at: str
    images: list[str] | None = None
    # Included, like `images` and unlike library_sources/workflow_steps above:
    # the line those exclusions draw is between the ANSWER and the private
    # facts about how it was produced (which documents were read, which models
    # ran, what each step cost). A generated video is the answer itself — the
    # thing the share link exists to show — so withholding it would hand the
    # recipient prose referring to a video they cannot see.
    videos: list[str] | None = None
    files: list[FileAttachment] | None = None
    sources: list[Source] | None = None
    search_queries: list[str] | None = None
    code_results: list[CodeResult] | None = None
    fact_checks: list[FactCheck] | None = None
    academic_results: list[AcademicResult] | None = None
    math_results: list[MathResult] | None = None

    @field_validator(
        "images",
        "videos",
        "files",
        "sources",
        "search_queries",
        "code_results",
        "fact_checks",
        "academic_results",
        "math_results",
        mode="before",
    )
    @classmethod
    def _parse_json_column(cls, value: object) -> object:
        # Same SQLite-stores-JSON-as-a-string handling as MessageOut's own
        # validator above.
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None


class SharedConversationOut(BaseModel):
    """The full body of a public GET /v1/shared/{token} response — a
    conversation's title and messages, nothing that identifies its owner."""

    title: str
    created_at: str
    messages: list[SharedMessage]


class ActionConfirmRequest(BaseModel):
    """Body for POST .../messages/{id}/action. confirm=false just declines
    (records the outcome, never touches the webhook)."""

    confirm: bool


class ActionResult(BaseModel):
    action_status: str
    detail: str | None = None


# Recorded voice clips: capped well above a typical few-minutes dictation clip
# but short of OpenAI's ~25MB file limit, to reject a pathological upload
# before it reaches the provider.
_MAX_AUDIO_CHARS = 34_000_000
_DATA_AUDIO_URL_RE = re.compile(
    r"^data:audio/(webm|wav|mp3|mpeg|mp4|m4a|ogg);base64,[A-Za-z0-9+/]+=*$"
)


class TranscribeRequest(BaseModel):
    audio: str = Field(
        ..., description="A recorded voice clip as data:audio/...;base64,..."
    )

    @field_validator("audio")
    @classmethod
    def _validate_audio(cls, value: str) -> str:
        if len(value) > _MAX_AUDIO_CHARS:
            raise ValueError("audio clip is too large")
        if not _DATA_AUDIO_URL_RE.match(value):
            raise ValueError(
                "audio must be a data:audio/{webm,wav,mp3,mpeg,mp4,m4a,ogg};base64,... URL"
            )
        return value


class TranscribeResponse(BaseModel):
    text: str


# Well above any reasonable chat answer, short of being a pathological upload
# — the actual OpenAI TTS input cap (4096 chars) is enforced server-side by
# truncating, not by rejecting the request (see speech.py).
_MAX_SPEECH_TEXT_CHARS = 50_000


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=_MAX_SPEECH_TEXT_CHARS)


class SpeechCostEstimate(BaseModel):
    """What a paid voice clip would cost, quoted BEFORE it is synthesized
    (GET /v1/speak/cost) — the figure the UI shows when it asks whether to
    spend, not a record of anything spent."""

    chars: int
    estimated_cost_usd: float
    model: str


class ClientErrorReport(BaseModel):
    """A browser-side crash report (see frontend/src/crashReporter.ts and
    POST /v1/client-errors). Pydantic caps here are generous transport
    guards against a pathological payload — the tighter STORED caps live in
    database.record_client_error as truncation, so a real but oversized
    stack loses its tail instead of the whole report being 422'd away."""

    message: str = Field(..., min_length=1, max_length=10_000)
    stack: str | None = Field(None, max_length=50_000)
    source_url: str | None = Field(None, max_length=4_000)


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def _trimmed_username(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 3:
            raise ValueError("username must be at least 3 characters after trimming")
        return trimmed


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # True when this account was admin-created/reset and hasn't set its own
    # password yet — the frontend steers into the change-password screen
    # before showing anything else.
    must_change_password: bool = False


class UserOut(BaseModel):
    id: int
    username: str
    created_at: str


class AdminUserOut(BaseModel):
    """A user row as shown in the admin user-management list. Never
    includes password_hash."""

    id: int
    username: str
    created_at: str
    is_active: bool
    must_change_password: bool
    last_login_at: str | None = None


class AdminCreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)

    @field_validator("username")
    @classmethod
    def _trimmed_username(cls, value: str) -> str:
        trimmed = value.strip()
        if len(trimmed) < 3:
            raise ValueError("username must be at least 3 characters after trimming")
        return trimmed


class AdminCreateUserResponse(BaseModel):
    user: AdminUserOut
    # Returned exactly once, here, and never logged or persisted in plain
    # text — the account is flagged must_change_password so its first
    # sign-in is forced to replace it.
    temporary_password: str


class ResetPasswordResponse(BaseModel):
    # Same one-time, never-logged contract as AdminCreateUserResponse.
    temporary_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8, max_length=128)


class SettingUpdate(BaseModel):
    # An empty value clears the override (reverts the key to its env/default).
    # The outer bound here is MAX_PROMPT_LEN (a role prompt is the longest
    # settable value); a model name's own tighter cap (settings.MAX_MODEL_LEN)
    # is still enforced separately by validate_model_value.
    value: str = Field(default="", max_length=MAX_PROMPT_LEN)


class SetupTestKeyRequest(BaseModel):
    """A candidate OPENAI_API_KEY to verify with one minimal call. Never
    persisted, logged, or echoed back — see app/routers/setup.py."""

    api_key: str = Field(..., min_length=1, max_length=512)


class SetupTestKeyResponse(BaseModel):
    """`ok` is what the wizard branches on; `outcome` says why, for wording.
    Every outcome that reached the provider and was not an auth rejection is
    `ok` — a throttled or parameter-rejected request has already had its
    credential accepted, which is the only thing being tested."""

    ok: bool
    outcome: Literal["ok", "auth_failed", "unreachable", "rate_limited", "error"]
    model: str
    key_env: str = "OPENAI_API_KEY"
    detail: str
