from __future__ import annotations

from pathlib import Path

import pytest

import app.orchestrator as orchestrator
from app.context_summary import summarize_conversation
from app.database import create_conversation, get_summary_cache
from app.routers.messages import (
    build_context_prompt,
    build_context_prompt_with_cache_split,
)
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


# --- build_context_prompt_with_cache_split -----------------------------------


def test_cache_split_returns_none_for_a_fresh_conversation() -> None:
    full, cacheable, remainder = build_context_prompt_with_cache_split([], "hi")
    assert full == "hi"
    assert cacheable is None
    assert remainder == "hi"


def test_cache_split_isolates_the_instructions_block() -> None:
    full, cacheable, remainder = build_context_prompt_with_cache_split(
        [], "hi", system_prompt="Be terse."
    )
    assert cacheable is not None
    assert "Be terse." in cacheable
    assert "Current user question" not in cacheable
    assert full == f"{cacheable}\n\nCurrent user question:\nhi"
    # The remainder is exactly what's left after stripping the cacheable
    # block off `full` — the piece Anthropic must send instead of `full`
    # whenever it also sends `cacheable` via the native system param, so the
    # instructions text is never billed twice.
    assert remainder == "Current user question:\nhi"
    assert full == f"{cacheable}\n\n{remainder}"


def test_cache_split_reconstructs_the_same_full_prompt_build_context_prompt_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    prior = [
        {"role": "user", "content": "hello there"},
        {"role": "assistant", "content": "hi!"},
    ]

    plain = build_context_prompt(prior, "q", system_prompt="Be terse.")
    full, cacheable, remainder = build_context_prompt_with_cache_split(
        prior, "q", system_prompt="Be terse."
    )

    assert full == plain
    assert cacheable is not None
    assert cacheable in full
    assert remainder not in cacheable  # no overlap between the two halves
    assert full == f"{cacheable}\n\n{remainder}"


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
    # No older turns exist at all, so the confident framing is accurate.
    assert "Do not claim you lack context" in prompt


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
    # Older turns exist but were never folded in (feature off) — the model
    # must not be told it has the full picture.
    assert "Do not claim you lack context" not in prompt
    assert "could not be summarized" in prompt


def test_summarizer_failure_uses_an_honest_context_caveat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")

    def failing(text: str) -> str:
        return ""  # summarize_text's own contract on any failure

    prior = [{"role": "user", "content": f"m-{i:02d}"} for i in range(1, 21)]
    prompt = build_context_prompt(prior, "q", summarize=failing)

    assert "Summary of earlier messages:" not in prompt
    # A silently-failed summary must not be papered over with a confident claim.
    assert "Do not claim you lack context" not in prompt
    assert "could not be summarized" in prompt


def test_successful_summary_keeps_the_confident_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")

    def fake(text: str) -> str:
        return "EARLIER: stuff happened"

    prior = [{"role": "user", "content": f"m-{i:02d}"} for i in range(1, 21)]
    prompt = build_context_prompt(prior, "q", summarize=fake)

    assert "Summary of earlier messages:" in prompt
    assert "Do not claim you lack context" in prompt
    assert "could not be summarized" not in prompt


# --- summary caching (conversation_id given) -----------------------------------


def test_build_context_prompt_does_not_fold_until_window_exceeds_max(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "should not be called"

    # 20 prior messages: over the old fixed-12 threshold, but under the
    # checkpoint scheme's _RECENT_WINDOW_MAX (24) — no fold should trigger yet.
    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 21)]
    prompt = build_context_prompt(prior, "q1", summarize=fake, conversation_id=conv_id)

    assert calls == []
    assert "Summary of earlier messages:" not in prompt
    assert get_summary_cache(conv_id) is None
    # All 20 prior messages are present verbatim — nothing summarized away.
    for i in range(1, 21):
        assert f"msg-{i:02d}" in prompt


def test_build_context_prompt_does_not_fold_for_a_small_window_of_short_messages(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity check for the token-based trigger added alongside the
    message-count one: a handful of ordinary short messages must not trip
    it."""
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "should not be called"

    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 16)]
    build_context_prompt(prior, "q1", summarize=fake, conversation_id=conv_id)

    assert calls == []
    assert get_summary_cache(conv_id) is None


def test_build_context_prompt_folds_early_when_recent_window_is_token_heavy(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handful of very long messages can blow past a reasonable context
    size long before the verbatim tail reaches _RECENT_WINDOW_MAX (24)
    messages — the token-count trigger must fold early in that case rather
    than wait for the message-count trigger to eventually catch up."""
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "summary-call-1"

    # 15 prior messages (well under _RECENT_WINDOW_MAX=24), but each is 2000
    # chars -> ~7500 approx-tokens total, over _RECENT_WINDOW_TOKEN_MAX (6000).
    prior = [
        {"role": "user", "content": f"msg-{i:02d}-" + ("x" * 2000)}
        for i in range(1, 16)
    ]
    prompt = build_context_prompt(prior, "q1", summarize=fake, conversation_id=conv_id)

    assert len(calls) == 1
    cached = get_summary_cache(conv_id)
    assert cached is not None
    assert cached["older_count"] == 15 - 12  # to_fold = recent_messages[:-12]
    assert "Summary of earlier messages:" in prompt


def test_build_context_prompt_folds_once_the_window_exceeds_max(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "summary-call-1"

    # 26 prior messages: past _RECENT_WINDOW_MAX (24) — folds the oldest
    # 26 - 12 = 14 messages into the summary, leaving the most recent 12
    # verbatim (the _RECENT_WINDOW_MIN the checkpoint trims back down to).
    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 27)]
    prompt = build_context_prompt(prior, "q1", summarize=fake, conversation_id=conv_id)

    assert len(calls) == 1
    assert "msg-01" in calls[0]
    assert "msg-14" in calls[0]

    cached = get_summary_cache(conv_id)
    assert cached is not None
    assert cached["older_count"] == 14
    assert cached["summary"] == "summary-call-1"

    assert "Summary of earlier messages:" in prompt
    for i in range(1, 15):
        assert f"msg-{i:02d}" not in prompt.split("Conversation history:", 1)[1]
    for i in range(15, 27):
        assert f"msg-{i:02d}" in prompt


def test_build_context_prompt_stays_stable_across_turns_within_the_checkpoint(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the checkpoint scheme: the system+summary prefix
    (what a provider's native prompt caching would key off) must stay
    BYTE-IDENTICAL turn over turn as long as the recent window hasn't grown
    past _RECENT_WINDOW_MAX — a strict every-turn sliding window would change
    this prefix every single call, defeating caching entirely.
    """
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return "summary-call-1"

    # First call pushes past the max and establishes a checkpoint at 14.
    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 27)]
    _, cacheable_1, _ = build_context_prompt_with_cache_split(
        prior, "q1", summarize=fake, conversation_id=conv_id
    )
    assert len(calls) == 1

    # Two more turns append 2 more messages each — still short of the next
    # fold threshold (checkpoint 14 + _RECENT_WINDOW_MAX 24 = 38). No further
    # summarizer call, and the cacheable system block must not change at all.
    prior_grown = prior + [
        {"role": "user", "content": "msg-27"},
        {"role": "assistant", "content": "msg-28"},
    ]
    _, cacheable_2, _ = build_context_prompt_with_cache_split(
        prior_grown, "q2", summarize=fake, conversation_id=conv_id
    )
    assert len(calls) == 1  # no second summarizer call
    assert cacheable_2 == cacheable_1  # byte-identical prefix — this is the win

    prior_grown_more = prior_grown + [
        {"role": "user", "content": "msg-29"},
        {"role": "assistant", "content": "msg-30"},
    ]
    _, cacheable_3, _ = build_context_prompt_with_cache_split(
        prior_grown_more, "q3", summarize=fake, conversation_id=conv_id
    )
    assert len(calls) == 1
    assert cacheable_3 == cacheable_1


def test_build_context_prompt_folds_again_once_the_next_window_is_exceeded(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUMMARIZE_HISTORY", "true")
    conv = create_conversation("t", None)
    conv_id = int(conv["id"])

    calls: list[str] = []

    def fake(text: str) -> str:
        calls.append(text)
        return f"summary-call-{len(calls)}"

    prior = [{"role": "user", "content": f"msg-{i:02d}"} for i in range(1, 27)]  # 26
    build_context_prompt(prior, "q1", summarize=fake, conversation_id=conv_id)
    assert len(calls) == 1
    assert get_summary_cache(conv_id)["older_count"] == 14

    # Grow past the next threshold: checkpoint(14) + _RECENT_WINDOW_MAX(24) = 38.
    prior_grown = prior + [
        {"role": "user", "content": f"msg-{i:02d}"} for i in range(27, 40)
    ]  # 39 total
    build_context_prompt(prior_grown, "q2", summarize=fake, conversation_id=conv_id)

    assert len(calls) == 2
    # Only the newly-aged-out delta was fed to the summarizer — not a
    # re-transcription of the whole older history from scratch.
    assert "msg-01" not in calls[1]
    assert "msg-15" in calls[1]
    # The previous summary was folded in, not discarded.
    assert "summary-call-1" in calls[1]

    cached_again = get_summary_cache(conv_id)
    assert cached_again["older_count"] == 39 - 12
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
