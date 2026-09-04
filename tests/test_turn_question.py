"""The raw user turn, not the assembled context prompt, is what every
turn-level read in the orchestrator sees (see `turn_req` in
app/orchestrator.py).

On a saved conversation `req.question` is the full composed prompt — system
preamble, memory, history, then the question — and the raw new turn arrives
separately as `routing_question`. Seen live: a "Fact-check: …" question on a
conversation with memory sent Google's claim search the whole prompt as the
claim and got HTTP 400. Each test here hands the orchestrator a composed
prompt plus the raw turn and asserts the side service saw only the turn.
"""

from __future__ import annotations

import pytest

import app.orchestrator as orchestrator
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode

PREAMBLE = (
    "You are AI Orchestrator, a cost-aware multi-model router. For questions "
    "about your own features, call the app_capabilities tool.\n\n"
    "<<<BEGIN REFERENCE MATERIAL>>>\n"
    '[From "Fact-check: is the Great Wall visible from space?" on 2026-09-04]\n'
    "Q: draw me a diagram of the wall\nA: I can't draw here.\n"
    "<<<END REFERENCE MATERIAL>>>\n\n"
    "Current user question:\n"
)


def _composed(turn: str) -> str:
    return PREAMBLE + turn


def _stub_model(monkeypatch: pytest.MonkeyPatch, answer: str = "ok") -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: answer)
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter([answer]))


# --- fact check --------------------------------------------------------------------


def test_fact_check_is_asked_about_the_turn_not_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_CHECK", "true")
    _stub_model(monkeypatch)
    seen: list[str] = []

    def fake_check_claim(query: str):
        seen.append(query)
        return []

    monkeypatch.setattr(orchestrator, "check_claim", fake_check_claim)
    turn = "Fact-check: is the Great Wall of China visible from space?"

    result = run_orchestrator(
        AskRequest(question=_composed(turn), mode=Mode.fast),
        routing_question=turn,
    )

    assert seen == [turn]
    assert "No published fact-checks matched" in result.answer


def test_stream_fact_check_is_asked_about_the_turn_not_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_CHECK", "true")
    _stub_model(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(orchestrator, "check_claim", lambda q: (seen.append(q), [])[1])
    turn = "Fact-check: is the Great Wall of China visible from space?"

    events = list(
        stream_orchestrator(
            AskRequest(question=_composed(turn), mode=Mode.fast),
            routing_question=turn,
        )
    )

    assert seen == [turn]
    done = next(e["data"] for e in events if e["event"] == "done")
    assert "No published fact-checks matched" in str(done["answer"])


def test_a_phrase_in_the_history_does_not_fire_the_fact_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reference material carries "Fact-check:" from a past turn; the new
    turn is arithmetic. The lookup must not run."""
    monkeypatch.setenv("FACT_CHECK", "true")
    _stub_model(monkeypatch, "4")
    monkeypatch.setattr(
        orchestrator,
        "check_claim",
        lambda q: pytest.fail(f"fact check fired on history text: {q!r}"),
    )

    result = run_orchestrator(
        AskRequest(question=_composed("what is 2+2"), mode=Mode.fast),
        routing_question="what is 2+2",
    )

    assert result.answer == "4"


# --- academic search --------------------------------------------------------------


def test_paper_search_is_asked_about_the_turn_not_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACADEMIC_SEARCH", "true")
    _stub_model(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        orchestrator, "search_papers", lambda q: (seen.append(q), [])[1]
    )
    turn = "find papers on transformer attention"

    run_orchestrator(
        AskRequest(question=_composed(turn), mode=Mode.fast),
        routing_question=turn,
    )

    assert seen == [turn]


# --- standalone image prompt ------------------------------------------------------


def test_the_image_model_is_given_the_turn_not_the_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    _stub_model(monkeypatch)
    seen: list[str] = []

    def fake_generate(model: str, prompt: str, *args: object, **kwargs: object):
        seen.append(prompt)
        return ["data:image/png;base64,QUJD"]

    monkeypatch.setattr(orchestrator, "generate_images_litellm", fake_generate)
    turn = "draw me a cat wearing a hat"

    result = run_orchestrator(
        AskRequest(question=_composed(turn), mode=Mode.fast),
        routing_question=turn,
    )

    assert seen == [turn]
    assert result.images == ["data:image/png;base64,QUJD"]


def test_a_draw_me_in_the_history_does_not_render_an_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    _stub_model(monkeypatch, "4")
    monkeypatch.setattr(
        orchestrator,
        "generate_images_litellm",
        lambda *a, **k: pytest.fail("image generation fired on history text"),
    )

    result = run_orchestrator(
        AskRequest(question=_composed("what is 2+2"), mode=Mode.fast),
        routing_question="what is 2+2",
    )

    assert result.answer == "4"


# --- the stateless endpoint is unchanged ------------------------------------------


def test_without_a_routing_question_the_request_question_is_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FACT_CHECK", "true")
    _stub_model(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(orchestrator, "check_claim", lambda q: (seen.append(q), [])[1])

    run_orchestrator(AskRequest(question="fact-check: is water wet?", mode=Mode.fast))

    assert seen == ["fact-check: is water wet?"]
