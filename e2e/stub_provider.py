"""A minimal stand-in for the OpenAI Responses API, used only by the
Playwright E2E smoke suite (see e2e/README.md). Speaks just enough of the
real wire protocol -- verified against the actual `openai` SDK client, not
guessed -- for `app.orchestrator_calls._call_openai`/`_stream_openai` to
parse a real response: a non-streaming JSON body and a streaming SSE body,
both built from the SDK's own Pydantic response models so the schema can't
drift from what the SDK expects.

The backend is pointed here via OPENAI_BASE_URL, which the openai-python
SDK honors natively (no application code changes needed). Every reply is
the same canned sentence; the point of this suite is proving the request
makes it end-to-end through real HTTP/SSE/auth/proxy plumbing, not testing
model behavior (that's covered by the backend's own mocked unit tests).
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from openai.types.responses import (
    Response,
    ResponseCodeInterpreterToolCall,
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_code_interpreter_tool_call import OutputLogs
from openai.types.responses.response_output_text import AnnotationContainerFileCitation
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
)

STUB_ANSWER = "Hello from the E2E stub."

# A question containing this word gets the code-execution-shaped reply below
# instead of the plain one -- the seam that lets an E2E test drive the real
# inline spreadsheet preview (MessageList.tsx's SpreadsheetPreviewBlock ->
# POST /v1/spreadsheet-preview) through the real app, which is otherwise
# unreachable without a model that actually runs code.
SPREADSHEET_TRIGGER = "spreadsheet"
SPREADSHEET_ANSWER = "Here is the spreadsheet you asked for."

STUB_CONTAINER_ID = "cntr_stub"
STUB_FILE_ID = "cfile_stub"
STUB_FILENAME = "quarterly_report.csv"

# Deliberately wider than any phone viewport and taller than the 50-row
# preview cap, so the E2E run exercises the parts of the preview that only
# appear under real overflow: horizontal scrolling inside the panel, the
# right-edge fade, the sticky header, the per-cell width cap, and the
# "showing first 50 of 120 rows" notice.
_SHEET_COLS = 12
_SHEET_ROWS = 120
_LONG_CELL = (
    "a deliberately long free-text cell that would stretch this table to "
    "thousands of pixels wide if the per-cell width cap were not doing its job"
)


def _stub_csv() -> bytes:
    header = ",".join(f"column_heading_{c}" for c in range(_SHEET_COLS))
    lines = [header]
    for r in range(1, _SHEET_ROWS):
        cells = [f"r{r}c{c}" for c in range(_SHEET_COLS)]
        if r == 1:
            cells[2] = f'"{_LONG_CELL}"'
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode()


def _make_response(model: str, *, spreadsheet: bool = False) -> Response:
    text = SPREADSHEET_ANSWER if spreadsheet else STUB_ANSWER
    annotations: list[AnnotationContainerFileCitation] = []
    output: list[object] = []
    if spreadsheet:
        # A generated non-image file surfaces as a container_file_citation
        # ANNOTATION on the answer text, not as an `outputs` entry on the
        # tool call -- see orchestrator_extract._extract_code_results, which
        # this shape exists to satisfy.
        annotations.append(
            AnnotationContainerFileCitation(
                type="container_file_citation",
                container_id=STUB_CONTAINER_ID,
                file_id=STUB_FILE_ID,
                filename=STUB_FILENAME,
                start_index=0,
                end_index=len(text),
            )
        )
        output.append(
            ResponseCodeInterpreterToolCall(
                id="ci_stub",
                type="code_interpreter_call",
                status="completed",
                container_id=STUB_CONTAINER_ID,
                code=f"df.to_csv({STUB_FILENAME!r}, index=False)",
                outputs=[OutputLogs(type="logs", logs="wrote quarterly_report.csv")],
            )
        )
    output.append(
        ResponseOutputMessage(
            id="msg_stub",
            type="message",
            role="assistant",
            status="completed",
            content=[
                ResponseOutputText(
                    type="output_text", text=text, annotations=annotations
                )
            ],
        )
    )
    return Response(
        id="resp_stub",
        created_at=0,
        model=model,
        object="response",
        status="completed",
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        output=output,  # type: ignore[arg-type]
        usage=ResponseUsage(
            input_tokens=5,
            output_tokens=3,
            total_tokens=8,
            input_tokens_details=InputTokensDetails(
                cached_tokens=0, cache_write_tokens=0
            ),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=0),
        ),
    )


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        model = body.get("model", "stub-model")
        # The router's classifier call goes through here too; keying off the
        # request body's text means only the answering call for a question
        # that actually asked for a spreadsheet gets the file-bearing shape.
        spreadsheet = SPREADSHEET_TRIGGER in json.dumps(body).lower()
        response = _make_response(model, spreadsheet=spreadsheet)
        answer = SPREADSHEET_ANSWER if spreadsheet else STUB_ANSWER

        if not body.get("stream"):
            payload = response.model_dump_json().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        created = ResponseCreatedEvent(
            response=response, sequence_number=0, type="response.created"
        )
        self._send_event(created)
        for index, char in enumerate(answer):
            delta = ResponseTextDeltaEvent(
                content_index=0,
                delta=char,
                item_id="msg_stub",
                logprobs=[],
                output_index=0,
                sequence_number=index + 1,
                type="response.output_text.delta",
            )
            self._send_event(delta)
        completed = ResponseCompletedEvent(
            response=response,
            sequence_number=len(answer) + 1,
            type="response.completed",
        )
        self._send_event(completed)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_event(self, event) -> None:
        self.wfile.write(f"data: {event.model_dump_json()}\n\n".encode())
        self.wfile.flush()

    def do_GET(self) -> None:
        # The containers Files API the backend calls to fetch a file a
        # sandbox run produced (orchestrator_extract._download_openai_code_file
        # -> client.containers.files.content.retrieve).
        if self.path.startswith("/v1/containers/") and self.path.endswith("/content"):
            payload = _stub_csv()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        # Readiness probe for the E2E harness (Playwright's webServer.url).
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # Keep CI logs focused on the actual test/backend/frontend output.


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8999
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"stub_provider listening on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
