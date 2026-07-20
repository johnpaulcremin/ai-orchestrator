from __future__ import annotations

import pytest

import app.orchestrator as orchestrator
from app.context_summary import summarize_conversation
from app.main import build_context_prompt
from app.orchestrator import summarize_text


# --- pure summary helper -----------------------------------------------------


def test_summarize_conversation_feeds_transcript_to_summarizer() -> None:
    older = [
        {"role": "user", "content": "Remember the number 42."},
        {"role": "assistant", "content": "Got it, 42."},
    ]
    seen: dict[str, str] = {}

    def fake(text: str) -> str:
        seen["text"] = text
        return "  Notes: the number is 42.  "

    out = summarize_conversation(older, fake)
    assert out == "Notes: the number is 42."  # trimmed
    assert "USER: Remember the number 42." in seen["text"]
    assert "ASSISTANT: Got it, 42." in seen["text"]


def test_summarize_conversation_empty_input_is_empty() -> None:
    assert summarize_conversation([], lambda _t: "x") == ""
    assert (
        summarize_conversation([{"role": "user", "content": "   "}], lambda _t: "x")
        == ""
    )


def test_summarize_conversation_survives_summarizer_failure() -> None:
    def boom(_text: str) -> str:
        raise RuntimeError("summarizer down")

    assert summarize_conversation([{"role": "user", "content": "hi"}], boom) == ""


# --- summarize_text (router call) graceful paths -----------------------------


def test_summarize_text_empty_input() -> None:
    assert summarize_text("") == ""
    assert summarize_text("   ") == ""


def test_summarize_text_without_client_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_client() -> object:
        raise RuntimeError("OPENAI_API_KEY is not set")

    monkeypatch.setattr(orchestrator, "get_client", no_client)
    assert summarize_text("something to summarize") == ""


class _FakeClient:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def with_options(self, **kwargs: object) -> "_FakeClient":
        self._captured["options"] = kwargs
        return self

    @property
    def responses(self) -> object:
        outer = self

        class _R:
            def create(self, **kwargs: object) -> object:
                outer._captured["input"] = kwargs.get("input")
                return type("Result", (), {"output_text": "the summary"})()

        return _R()


def test_summarize_text_keeps_the_recent_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "get_client", lambda: _FakeClient(captured))

    # Older window bigger than the input cap: the oldest marker must be dropped
    # and the most-recent marker kept (recency matters more).
    text = "OLDEST_MARKER " + ("x " * 20000) + " NEWEST_MARKER"
    assert summarize_text(text) == "the summary"
    sent = str(captured["input"])
    assert "NEWEST_MARKER" in sent
    assert "OLDEST_MARKER" not in sent


def test_summarize_text_uses_a_bounded_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(orchestrator, "get_client", lambda: _FakeClient(captured))

    summarize_text("summarize me")
    # Fail-fast: no SDK retries and a short timeout so it can't stall the answer.
    assert captured["options"].get("max_retries") == 0
    assert captured["options"].get("timeout", 999) <= 15


# --- build_context_prompt integration ----------------------------------------


def test_long_history_folds_in_a_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 21)]  # 20
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "EARLIER: the user counted upward"

    prompt = build_context_prompt(prior, "current question", summarize=fake)

    # The summary block is present and the older turns were fed to the summarizer.
    assert "Summary of earlier messages:" in prompt
    assert "EARLIER: the user counted upward" in prompt
    assert "msg-01" in calls[0]  # oldest went to the summarizer, not verbatim

    # The recent 12 (msg-09..msg-20) are still present verbatim.
    for i in range(9, 21):
        assert f"msg-{i:02d}" in prompt
    # The oldest (summarized) turns are NOT in the verbatim tail.
    tail = prompt.split("Conversation history:", 1)[1]
    assert "msg-01" not in tail
    assert "current question" in prompt


def test_short_history_never_summarizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    called: list[str] = []

    def fake(text: str) -> str:
        called.append(text)
        return "should not appear"

    prior = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]
    prompt = build_context_prompt(prior, "q", summarize=fake)
    assert called == []  # <= 12 prior messages: the summarizer is never invoked
    assert "Summary of earlier messages:" not in prompt


def test_disabled_flag_skips_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "false")
    called: list[str] = []

    def fake(text: str) -> str:
        called.append(text)
        return "nope"

    prior = [{"role": "user", "content": f"m-{i:02d}"} for i in range(1, 21)]
    prompt = build_context_prompt(prior, "q", summarize=fake)
    assert called == []
    assert "Summary of earlier messages:" not in prompt
