from __future__ import annotations

from app.main import build_context_prompt


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
