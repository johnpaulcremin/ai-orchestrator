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
    ResponseCompletedEvent,
    ResponseCreatedEvent,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseTextDeltaEvent,
    ResponseUsage,
)
from openai.types.responses.response_usage import (
    InputTokensDetails,
    OutputTokensDetails,
)

STUB_ANSWER = "Hello from the E2E stub."


def _make_response(model: str) -> Response:
    return Response(
        id="resp_stub",
        created_at=0,
        model=model,
        object="response",
        status="completed",
        parallel_tool_calls=False,
        tool_choice="auto",
        tools=[],
        output=[
            ResponseOutputMessage(
                id="msg_stub",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text", text=STUB_ANSWER, annotations=[]
                    )
                ],
            )
        ],
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
        response = _make_response(model)

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
        for index, char in enumerate(STUB_ANSWER):
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
            sequence_number=len(STUB_ANSWER) + 1,
            type="response.completed",
        )
        self._send_event(completed)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _send_event(self, event) -> None:
        self.wfile.write(f"data: {event.model_dump_json()}\n\n".encode())
        self.wfile.flush()

    def do_GET(self) -> None:
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
