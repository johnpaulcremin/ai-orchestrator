from __future__ import annotations

from app.routers.messages import build_context_prompt


def test_no_history_returns_bare_question() -> None:
    question = "What is the capital of France?"
    assert build_context_prompt([], question) == question


def test_roles_uppercased_and_empty_content_skipped() -> None:
    prior = [
        {"role": "user", "content": "Hello there"},
        {"role": "assistant", "content": "   "},
        {"role": "assistant", "content": "Hi!"},
    ]

    prompt = build_context_prompt(prior, "Next question")

    assert "USER: Hello there" in prompt
    assert "ASSISTANT: Hi!" in prompt
    # The whitespace-only message contributes no history line.
    assert prompt.count("ASSISTANT:") == 1

    assert "Conversation history:" in prompt
    assert "Current user question:" in prompt
    assert prompt.rstrip().endswith("Next question")


def test_truncates_to_last_twelve_messages() -> None:
    prior = [{"role": "user", "content": f"message-{i:02d}"} for i in range(1, 21)]

    prompt = build_context_prompt(prior, "current question")

    # The first 8 of 20 messages fall outside the 12-message window.
    for i in range(1, 9):
        assert f"message-{i:02d}" not in prompt

    for i in range(9, 21):
        assert f"message-{i:02d}" in prompt

    assert "current question" in prompt


# --- per-turn note lines must not re-enter a prompt as history ----------------

# The note an assistant answer carries when self-description fired on ITS
# turn. True then; expired the moment that turn ended. Observed live: folded
# back in as history, a later budget-tier turn read the tools list as current
# and answered "I can't generate images" with IMAGE_GENERATION on.
_ANSWER_WITH_PER_TURN_NOTE = (
    "Here is the plan for your garden.\n"
    "- Answering YOU right now — gpt-5. Do not infer the provider otherwise.\n"
    "- Tools actually available to YOU on this turn — code execution, "
    "precision math (SymPy). This list is the authority on what you can do "
    "right now.\n"
    "- Enabled optional features — IMAGE_GENERATION, MATH_SOLVE.\n"
    "- Your remaining daily budget — $2.1400"
)


def test_history_fold_strips_per_turn_note_lines() -> None:
    prior = [
        {"role": "user", "content": "plan my garden"},
        {"role": "assistant", "content": _ANSWER_WITH_PER_TURN_NOTE},
    ]
    prompt = build_context_prompt(prior, "now draw it for me")

    assert "Tools actually available to YOU on this turn" not in prompt
    assert "Answering YOU right now" not in prompt
    assert "remaining daily budget" not in prompt
    # The stable lines and the actual answer stay: history is filtered, not
    # amputated.
    assert "Here is the plan for your garden." in prompt
    assert "Enabled optional features" in prompt


def test_history_fold_leaves_user_messages_alone() -> None:
    """A USER quoting those very words is content, not an expired note."""
    prior = [
        {
            "role": "user",
            "content": 'why did you say "Tools actually available to YOU on this turn"?',
        },
        {"role": "assistant", "content": "Because self-description fired."},
    ]
    prompt = build_context_prompt(prior, "ok")
    assert "Tools actually available to YOU on this turn" in prompt


def test_router_snippet_strips_per_turn_note_lines() -> None:
    from app.context_builder import build_recent_history_snippet

    snippet = build_recent_history_snippet(
        [{"role": "assistant", "content": _ANSWER_WITH_PER_TURN_NOTE}]
    )
    assert "Tools actually available" not in snippet
    assert "Here is the plan for your garden." in snippet


def test_summary_input_strips_per_turn_note_lines() -> None:
    """Summarized, an expired tools list would smuggle itself into every
    later prompt as settled fact — worse than one stale fold, permanent."""
    from app.context_summary import summarize_conversation

    seen: list[str] = []

    def fake_summarize(text: str) -> str:
        seen.append(text)
        return "summary"

    summarize_conversation(
        [
            {"role": "user", "content": "plan my garden"},
            {"role": "assistant", "content": _ANSWER_WITH_PER_TURN_NOTE},
        ],
        fake_summarize,
    )
    assert seen, "the summarizer must have been called"
    assert "Tools actually available" not in seen[0]
    assert "Here is the plan for your garden." in seen[0]
