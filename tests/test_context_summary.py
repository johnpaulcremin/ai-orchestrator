from __future__ import annotations

from pathlib import Path

import pytest

import app.orchestrator as orchestrator
from app.context_summary import summarize_conversation
from app.database import create_conversation, get_summary_cache
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


# --- folding a delta into a previous summary ----------------------------------


def test_summarize_conversation_folds_delta_into_previous_summary() -> None:
    new_messages = [{"role": "user", "content": "the sequel"}]
    seen: dict[str, str] = {}

    def fake(text: str) -> str:
        seen["text"] = text
        return "Updated notes."

    out = summarize_conversation(new_messages, fake, previous_summary="Earlier notes.")
    assert out == "Updated notes."
    # Both the prior summary and only the NEW messages went to the summarizer —
    # not a re-transcription of the whole older history.
    assert "Earlier notes." in seen["text"]
    assert "the sequel" in seen["text"]


def test_summarize_conversation_no_new_messages_keeps_previous_summary_unchanged() -> (
    None
):
    called = []

    def fake(text: str) -> str:
        called.append(text)
        return "should not be called"

    out = summarize_conversation([], fake, previous_summary="Earlier notes.")
    assert out == "Earlier notes."
    assert called == []  # nothing new to fold in, so no call at all


def test_summarize_conversation_fold_failure_keeps_previous_summary() -> None:
    def boom(_text: str) -> str:
        raise RuntimeError("summarizer down")

    out = summarize_conversation(
        [{"role": "user", "content": "new stuff"}],
        boom,
        previous_summary="Earlier notes.",
    )
    assert out == "Earlier notes."


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


# --- summary caching (conversation_id given) -----------------------------------


def test_build_context_prompt_caches_and_folds_only_the_new_delta(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return f"summary-call-{len(calls)}"

    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 21)]  # 20
    build_context_prompt(prior, "q1", summarize=fake, conversation_id=conv_id)
    assert len(calls) == 1
    assert "msg-01" in calls[0]  # first call: the whole older set (msg-01..08)

    cached = get_summary_cache(conv_id)
    assert cached is not None
    assert cached["older_count"] == 8
    assert cached["summary"] == "summary-call-1"

    # Two more turns append 2 messages, shifting the recent-12 window forward
    # and aging msg-09/msg-10 into the older set.
    prior_grown = prior + [
        {"role": "user", "content": "msg-21"},
        {"role": "assistant", "content": "msg-22"},
    ]
    build_context_prompt(prior_grown, "q2", summarize=fake, conversation_id=conv_id)
    assert len(calls) == 2
    # Only the newly-aged-out delta was fed to the summarizer this time — not
    # a re-transcription of the whole older history from scratch.
    assert "msg-01" not in calls[1]
    assert "msg-09" in calls[1]
    assert "msg-10" in calls[1]
    # The previous summary was folded in, not discarded.
    assert "summary-call-1" in calls[1]

    cached_again = get_summary_cache(conv_id)
    assert cached_again is not None
    assert cached_again["older_count"] == 10
    assert cached_again["summary"] == "summary-call-2"


def test_build_context_prompt_without_conversation_id_never_caches(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "a summary"

    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 21)]
    # regenerate/edit call sites omit conversation_id — every call must
    # re-summarize the whole older set from scratch, matching their
    # reconstructed (non-monotonic) `prior_messages`.
    build_context_prompt(prior, "q1", summarize=fake)
    build_context_prompt(prior, "q2", summarize=fake)
    assert len(calls) == 2
    assert "msg-01" in calls[0]
    assert "msg-01" in calls[1]
