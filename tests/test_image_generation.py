"""Image generation: the image_generation tool gating/config, extraction, cost
estimation, the cache-skip invariant, and end-to-end persistence.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
from app import cache
from app.database import add_message, create_conversation, list_messages
from app.orchestrator import (
    _build_image_generation_tool,
    _extract_images,
    _image_generation_enabled,
    _image_generation_model,
    _image_generation_provider,
    _image_generation_quality,
    _image_generation_size,
    _looks_like_image_request,
    prefers_drawn_by_code,
    run_orchestrator,
    stream_orchestrator,
)
from app.providers import generate_images_litellm
from app.schemas import AskRequest, Mode
from app.usage import estimate_image_cost


# --- usage.py: estimate_image_cost -------------------------------------------


def test_estimate_image_cost_zero_count_is_none() -> None:
    assert estimate_image_cost(0, "high") is None


def test_estimate_image_cost_uses_quality_default() -> None:
    assert estimate_image_cost(2, "low") == pytest.approx(0.04)


def test_estimate_image_cost_unknown_quality_falls_back_to_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_COST_USD", raising=False)
    assert estimate_image_cost(1, "nonsense") == estimate_image_cost(1, "auto")


def test_estimate_image_cost_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_COST_USD", "0.5")
    assert estimate_image_cost(3, "high") == pytest.approx(1.5)


def test_estimate_image_cost_invalid_override_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_COST_USD", "not-a-number")
    assert estimate_image_cost(1, "high") == pytest.approx(0.19)


# --- orchestrator: config helpers ---------------------------------------------


def test_image_generation_enabled_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GENERATION", raising=False)
    assert _image_generation_enabled() is False
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    assert _image_generation_enabled() is True
    monkeypatch.setenv("IMAGE_GENERATION", "false")
    assert _image_generation_enabled() is False


def test_image_generation_model_default_and_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    assert _image_generation_model() == "gpt-image-1"
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gpt-image-1.5")
    assert _image_generation_model() == "gpt-image-1.5"


def test_image_generation_quality_default_is_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_QUALITY", raising=False)
    assert _image_generation_quality() == "high"


def test_image_generation_quality_invalid_falls_back_to_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "ultra")
    assert _image_generation_quality() == "high"


def test_image_generation_quality_accepts_valid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "low")
    assert _image_generation_quality() == "low"


def test_image_generation_size_default_is_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_SIZE", raising=False)
    assert _image_generation_size() == "auto"
    monkeypatch.setenv("IMAGE_GENERATION_SIZE", "1024x1536")
    assert _image_generation_size() == "1024x1536"


def test_build_image_generation_tool_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gpt-image-1")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setenv("IMAGE_GENERATION_SIZE", "auto")
    assert _build_image_generation_tool() == {
        "type": "image_generation",
        "model": "gpt-image-1",
        "quality": "high",
        "size": "auto",
    }


# --- orchestrator: _image_generation_provider (OpenAI tool vs Gemini direct) --


def test_image_generation_provider_defaults_to_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    assert _image_generation_provider() == "openai"


def test_image_generation_provider_gemini_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    assert _image_generation_provider() == "gemini"


def test_image_generation_provider_other_openai_model_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gpt-image-1.5")
    assert _image_generation_provider() == "openai"


# --- orchestrator: _looks_like_image_request ----------------------------------


@pytest.mark.parametrize(
    "question",
    [
        # The wordings the flat phrase list already covered.
        "draw me a cat wearing a hat",
        "Draw a picture of a sunset",
        "generate an image of a robot",
        "generate a picture of the ocean",
        "create an image of a mountain",
        "make me a picture of a dog",
        "paint a picture of a forest",
        "illustrate a spaceship",
        "sketch a portrait of a king",
        "design a logo for my startup",
    ],
)
def test_looks_like_image_request_positive(question: str) -> None:
    assert _looks_like_image_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # Verbs the enumerated list never had an entry for.
        "produce an image of a lighthouse",
        "render an illustration of a castle",
        "show me a diagram of the architecture",
        "give me an icon for the app",
        "whip up a poster for the launch",
        # Nouns it never had an entry for — what people actually ask for.
        "create a diagram of how this app routes a request",
        "make a mockup of the settings page",
        "generate a flowchart of the flow",
        "design a banner for the readme",
        "create an infographic about routing",
        "draw a map of the island",
        # An adjective between the article and the noun used to break it.
        "generate a high resolution image of a robot",
        "make me a quick sketch of the layout",
        # The verb carrying the request on its own.
        "draw yourself",
        "illustrate this for me",
        # The question that started this, and its correct spelling. "imagine"
        # is a real word, so no spellchecker would have flagged it — in the
        # noun slot it can only be the typo, so it is accepted there.
        "Can you create an imagine similar to this to show this app's make up?",
        "Can you create an image similar to this to show this app's make up?",
        "So can you draw an image of yourself similar to the example I attached?",
    ],
)
def test_looks_like_image_request_newly_covered(question: str) -> None:
    assert _looks_like_image_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "what is the current weather",
        "explain how photosynthesis works",
        "review my current implementation",
        "what is 2+2",
        "write a Python function to sort a list",
    ],
)
def test_looks_like_image_request_negative(question: str) -> None:
    assert _looks_like_image_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        # The flat list matched "draw a"/"draw an"/"draw me" on the verb
        # alone, so every one of these bought an image. Widening the verbs
        # without fixing it would have multiplied the class.
        "draw a conclusion from these numbers",
        "can you draw an analogy here",
        "draw the line between refactor and rewrite",
        "draw a comparison between SQLite and Postgres",
        # The object fronted, leaving an innocent word in the head position.
        "what conclusions do you draw a year later",
        # A preposition in the head position is never a thing anyone can draw.
        "what do you draw from this data",
        "draw up a plan for the migration",
        "draw on your own experience here",
    ],
)
def test_looks_like_image_request_abstract_sense_of_a_picture_verb(
    question: str,
) -> None:
    assert _looks_like_image_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        # A picture-noun behind a maker verb, but plainly a coding request.
        "create a function that returns an image buffer",
        "make a plan for the migration",
        "produce a report on spend",
        "design a schema for the messages table",
        "show me the routing code",
        "generate a csv of the spend log",
        # Data visualisations belong to code execution — a real chart from
        # real numbers, not an image model's impression of one.
        "plot a chart of daily spend",
        "create a graph of token usage",
    ],
)
def test_looks_like_image_request_stays_out_of_neighbouring_features(
    question: str,
) -> None:
    assert _looks_like_image_request(question) is False


# --- providers: generate_images_litellm ---------------------------------------


def test_generate_images_litellm_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.providers as providers

    fake_litellm = SimpleNamespace(
        image_generation=lambda **_kw: SimpleNamespace(
            data=[SimpleNamespace(b64_json="aaa"), SimpleNamespace(b64_json="bbb")]
        )
    )
    monkeypatch.setattr(providers, "_litellm", lambda: fake_litellm)

    images = generate_images_litellm(
        "gemini/imagen-4.0-generate-001", "a cat", "high", "auto"
    )
    assert images == [
        "data:image/png;base64,aaa",
        "data:image/png;base64,bbb",
    ]


def test_generate_images_litellm_omits_response_format_for_gpt_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpt-image-* rejects response_format outright, and LiteLLM does not drop
    it (it is a valid OpenAI image param, just not one that model accepts), so
    sending it 400s every call on the default backend."""
    import app.providers as providers

    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json="aaa")])

    monkeypatch.setattr(
        providers, "_litellm", lambda: SimpleNamespace(image_generation=capture)
    )

    images = generate_images_litellm("gpt-image-1", "a cat", "high", "auto")
    assert images == ["data:image/png;base64,aaa"]
    assert "response_format" not in seen


@pytest.mark.parametrize(
    "model",
    [
        "gpt-image-1",
        "GPT-Image-1",
        "gpt-image-1.5",
        # provider_of() routes EVERY prefixed name through LiteLLM, so the
        # same model is reachable under a prefix and rejects the parameter
        # just as hard there.
        "openai/gpt-image-1",
        "azure/gpt-image-1",
    ],
)
def test_generate_images_litellm_omits_response_format_under_any_prefix(
    model: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.providers as providers

    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json="aaa")])

    monkeypatch.setattr(
        providers, "_litellm", lambda: SimpleNamespace(image_generation=capture)
    )

    generate_images_litellm(model, "a cat", "high", "auto")
    assert "response_format" not in seen


def test_generate_images_litellm_keeps_response_format_for_other_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every non-gpt-image model needs it, or it returns a URL the frontend
    cannot render inline."""
    import app.providers as providers

    seen: dict[str, object] = {}

    def capture(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json="aaa")])

    monkeypatch.setattr(
        providers, "_litellm", lambda: SimpleNamespace(image_generation=capture)
    )

    generate_images_litellm("gemini/imagen-4.0-generate-001", "a cat", "high", "auto")
    assert seen["response_format"] == "b64_json"


def test_generate_images_litellm_failure_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers as providers

    def raise_error(**_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        providers, "_litellm", lambda: SimpleNamespace(image_generation=raise_error)
    )

    assert (
        generate_images_litellm(
            "gemini/imagen-4.0-generate-001", "a cat", "high", "auto"
        )
        == []
    )


def test_generate_images_litellm_skips_entries_without_b64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers as providers

    fake_litellm = SimpleNamespace(
        image_generation=lambda **_kw: SimpleNamespace(
            data=[SimpleNamespace(b64_json=None), SimpleNamespace(b64_json="ok")]
        )
    )
    monkeypatch.setattr(providers, "_litellm", lambda: fake_litellm)

    images = generate_images_litellm(
        "gemini/imagen-4.0-generate-001", "a cat", "high", "auto"
    )
    assert images == ["data:image/png;base64,ok"]


# --- orchestrator: Gemini direct-call wiring ----------------------------------


def test_run_orchestrator_gemini_path_generates_images_when_phrase_matches(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")
    monkeypatch.setattr(
        orchestrator,
        "generate_images_litellm",
        lambda *a, **k: ["data:image/png;base64,aaa"],
    )

    result = run_orchestrator(
        AskRequest(question="draw me a cat wearing a hat", mode=Mode.smart)
    )
    assert result.images == ["data:image/png;base64,aaa"]
    assert "Here you go." in result.answer


def test_run_orchestrator_gemini_path_skipped_without_matching_phrase(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "42")

    def boom(*_a, **_k):
        raise AssertionError("must not call the image API for an ordinary question")

    monkeypatch.setattr(orchestrator, "generate_images_litellm", boom)

    result = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.smart))
    assert result.images is None


def test_run_orchestrator_gemini_path_synthesizes_note_when_answer_empty(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "")
    monkeypatch.setattr(
        orchestrator,
        "generate_images_litellm",
        lambda *a, **k: ["data:image/png;base64,aaa"],
    )

    result = run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert result.answer.strip()
    assert result.images == ["data:image/png;base64,aaa"]


def test_run_orchestrator_gemini_path_independent_of_resolved_model_provider(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Gemini image path isn't a chat-model tool, so it must fire even when
    the resolved TEXT model is Claude/Anthropic, unlike the OpenAI tool path
    which requires provider_of(decision.model) == "openai"."""
    from app.routing import RouteDecision

    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    decision = RouteDecision(
        model="claude-sonnet-5",
        mode_used="auto->smart",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="medium",
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")
    monkeypatch.setattr(
        orchestrator,
        "generate_images_litellm",
        lambda *a, **k: ["data:image/png;base64,aaa"],
    )

    result = run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert result.images == ["data:image/png;base64,aaa"]


def test_openai_backend_falls_back_to_standalone_call_on_a_non_openai_model(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default backend (gpt-image-1) is a tool only an OpenAI model can be
    offered. An image request the router sends to Claude used to produce no
    image at all — no tool offered, and the standalone path was Gemini-only.
    It now takes the standalone call, same as the Gemini backend does.
    """
    from app.routing import RouteDecision

    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    decision = RouteDecision(
        model="claude-sonnet-5",
        mode_used="auto->smart",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="medium",
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs):
        seen["images"] = kwargs["images"]
        return "Here you go."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    def fake_generate(model, *_a, **_k):
        seen["image_model"] = model
        return ["data:image/png;base64,aaa"]

    monkeypatch.setattr(orchestrator, "generate_images_litellm", fake_generate)

    result = run_orchestrator(
        AskRequest(question="draw an image of yourself", mode=Mode.smart)
    )
    assert seen["images"] is False, "an Anthropic model can't be offered the tool"
    assert result.images == ["data:image/png;base64,aaa"]
    assert seen["image_model"] == "gpt-image-1"


def test_openai_backend_on_an_openai_model_still_uses_the_hosted_tool(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback must not double up: when the tool IS offered, the
    standalone call stays out of it (one image request, one image bill)."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen: dict[str, object] = {}

    def fake_call_model(**kwargs):
        seen["images"] = kwargs["images"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    def boom(*_a, **_k):
        raise AssertionError("the hosted tool already covers this turn")

    monkeypatch.setattr(orchestrator, "generate_images_litellm", boom)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["images"] is True


def test_standalone_fallback_still_needs_a_matching_phrase(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new fallback widens WHICH models can serve an image request, not
    WHICH questions count as one — an ordinary question routed to Claude must
    not start paying for images."""
    from app.routing import RouteDecision

    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    decision = RouteDecision(
        model="claude-sonnet-5",
        mode_used="auto->smart",
        notes="n",
        max_output_tokens=100,
        reasoning_effort="medium",
    )
    monkeypatch.setattr(orchestrator, "decide_route", lambda *a, **k: decision)
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "42")

    def boom(*_a, **_k):
        raise AssertionError("must not call the image API for an ordinary question")

    monkeypatch.setattr(orchestrator, "generate_images_litellm", boom)

    result = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.smart))
    assert result.images is None


def test_run_orchestrator_says_so_when_the_image_call_returns_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_images_litellm never raises — a refused key, a bad model
    name or an outage all come back as []. That used to vanish entirely: the
    user asked for a picture, got prose, and the answering model (never told
    the call happened) could only guess when asked "where's the image?"."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")
    monkeypatch.setattr(orchestrator, "generate_images_litellm", lambda *a, **k: [])

    result = run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))

    assert result.images is None
    assert "couldn't be generated" in result.answer
    assert "gemini/imagen-4.0-generate-001" in result.answer


def test_run_orchestrator_stays_quiet_when_no_image_was_ever_wanted(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure note is owed only where a call was actually made."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "42")

    result = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.smart))

    assert "couldn't be generated" not in result.answer


def test_stream_orchestrator_says_so_when_the_image_call_returns_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_stream_model", lambda **_kw: iter(["Here you go."])
    )
    monkeypatch.setattr(orchestrator, "generate_images_litellm", lambda *a, **k: [])

    events = list(
        stream_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert "couldn't be generated" in done["data"]["answer"]


def test_stream_orchestrator_gemini_path_yields_note_and_images(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_stream_model", lambda **_kw: iter(["Here you go."])
    )
    monkeypatch.setattr(
        orchestrator,
        "generate_images_litellm",
        lambda *a, **k: ["data:image/png;base64,aaa"],
    )

    events = list(
        stream_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["images"] == ["data:image/png;base64,aaa"]
    assert "Here you go." in done["data"]["answer"]


# --- orchestrator: _extract_images --------------------------------------------


def _fake_image_call(status: str, result: str | None) -> object:
    return SimpleNamespace(type="image_generation_call", status=status, result=result)


def test_extract_images_completed_with_result() -> None:
    result = SimpleNamespace(output=[_fake_image_call("completed", "YmFzZTY0")])
    assert _extract_images(result) == ["data:image/png;base64,YmFzZTY0"]


def test_extract_images_ignores_non_completed_status() -> None:
    result = SimpleNamespace(output=[_fake_image_call("in_progress", "YmFzZTY0")])
    assert _extract_images(result) == []


def test_extract_images_ignores_missing_result() -> None:
    result = SimpleNamespace(output=[_fake_image_call("completed", None)])
    assert _extract_images(result) == []


def test_extract_images_ignores_other_output_items() -> None:
    result = SimpleNamespace(
        output=[SimpleNamespace(type="function_call", status="completed", result=None)]
    )
    assert _extract_images(result) == []


def test_extract_images_no_output_attr() -> None:
    assert _extract_images(SimpleNamespace()) == []


def test_extract_images_multiple_calls() -> None:
    result = SimpleNamespace(
        output=[
            _fake_image_call("completed", "aaa"),
            _fake_image_call("completed", "bbb"),
        ]
    )
    assert _extract_images(result) == [
        "data:image/png;base64,aaa",
        "data:image/png;base64,bbb",
    ]


# --- orchestrator: gating + cost + cache-skip + response wiring --------------


def test_run_orchestrator_passes_images_true_only_when_enabled_and_openai(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen = {}

    def fake_call_model(**kwargs):
        seen["images"] = kwargs["images"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["images"] is True


def test_run_orchestrator_images_false_when_not_configured(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("IMAGE_GENERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    seen = {}
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kwargs: seen.setdefault("images", kwargs["images"]) or "ok",
    )

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert seen["images"] is False


def test_run_orchestrator_populates_images_and_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_QUALITY", "high")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        kwargs["generated_images"].append("data:image/png;base64,aaa")
        return "Here's the image you asked for."

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    result = run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    assert result.images == ["data:image/png;base64,aaa"]
    assert result.cost_usd == pytest.approx(0.19)


def test_run_orchestrator_skips_cache_when_images_generated(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs):
        kwargs["generated_images"].append("data:image/png;base64,aaa")
        return "note"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    run_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))

    key = cache.make_key("draw a cat", "smart")
    assert cache.get(key) is None


def test_stream_orchestrator_done_frame_includes_images(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_stream_model(**kwargs):
        kwargs["generated_images"].append("data:image/png;base64,aaa")
        yield "note"

    monkeypatch.setattr(orchestrator, "_stream_model", fake_stream_model)

    events = list(
        stream_orchestrator(AskRequest(question="draw a cat", mode=Mode.smart))
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["images"] == ["data:image/png;base64,aaa"]


def test_stream_orchestrator_omits_images_key_when_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["hi"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))
    done = events[-1]
    assert "images" not in done["data"]


# --- database: images persistence ---------------------------------------------


def test_add_message_and_list_messages_roundtrip_images(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="here you go",
        images=json.dumps(["data:image/png;base64,aaa"]),
    )
    messages = list_messages(conv["id"])
    assert json.loads(messages[0]["images"]) == ["data:image/png;base64,aaa"]


def test_add_message_without_images_stores_null(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(conversation_id=conv["id"], role="assistant", content="hi")
    messages = list_messages(conv["id"])
    assert messages[0]["images"] is None


# --- HTTP integration: images persist through ask / stream --------------------


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_conversation_persists_and_returns_images(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    def fake_run(req, routing_question=None, owner=None, history="", **_kw):
        return AskResponse(
            answer="Here's the image you asked for.",
            mode_used="smart",
            notes="n",
            images=["data:image/png;base64,aaa"],
        )

    monkeypatch.setattr("app.routers.messages.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(f"/v1/conversations/{cid}/ask", json={"question": "draw a cat"})

    assert r.status_code == 200
    assert r.json()["images"] == ["data:image/png;base64,aaa"]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["images"] == ["data:image/png;base64,aaa"]


def test_stream_ask_persists_images_from_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, history="", **_kw):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "Here's the image you asked for.",
                "mode_used": "smart",
                "notes": "n",
                "images": ["data:image/png;base64,aaa"],
            },
        }

    monkeypatch.setattr("app.routers.messages.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream", json={"question": "draw a cat"}
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["images"] == ["data:image/png;base64,aaa"]


# --- a diagram is better drawn by code than imagined --------------------------


@pytest.mark.parametrize(
    "question",
    [
        "Can you produce a diagram showing this app's makeup?",
        "draw me a diagram of the routing flow",
        "create a flowchart of the request lifecycle",
        "sketch the architecture for me",
        # chart/graph/plot were excluded from the picture-NOUN list, but the
        # verb rule still carried them to the image path. This closes that.
        "draw me a chart of daily spend",
        "draw a graph of token usage",
    ],
)
def test_a_structural_drawing_prefers_code_execution(question: str) -> None:
    """Observed live: asked for a diagram of this app, Claude wrote SVG
    programmatically and delivered a real hub-and-spoke drawing with legible
    labels. An image model asked the same returns an impression of one with
    the text garbled — for $0.19. A diagram is a drawing of a STRUCTURE, and
    structure survives being drawn by code in a way it does not survive being
    imagined."""
    assert prefers_drawn_by_code(question) is True


@pytest.mark.parametrize(
    "question",
    [
        "draw me a cat wearing a hat",
        "generate an image of a robot",
        "design a logo for my startup",
        "create a poster for the launch",
        "paint a picture of a forest",
        "illustrate a spaceship",
    ],
)
def test_a_pictorial_request_still_goes_to_the_image_model(question: str) -> None:
    """The exclusion is for structure, not for pictures. Code execution has
    nothing to offer a cat in a hat."""
    assert prefers_drawn_by_code(question) is False
    assert _looks_like_image_request(question) is True


def test_diagram_still_reaches_the_image_model_without_code_execution(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where code execution is NOT available to the answering model, an image
    model is the only instrument there is, and a mediocre diagram beats
    none — so the preference must be conditional, not absolute."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.delenv("CODE_EXECUTION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")
    monkeypatch.setattr(
        orchestrator,
        "generate_images_litellm",
        lambda *a, **k: ["data:image/png;base64,aaa"],
    )

    result = run_orchestrator(
        AskRequest(question="draw me a diagram of the flow", mode=Mode.smart)
    )
    assert result.images == ["data:image/png;base64,aaa"]


def test_diagram_skips_the_image_call_when_code_execution_is_available(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")

    def boom(*_a, **_k):
        raise AssertionError("code execution draws this one properly")

    monkeypatch.setattr(orchestrator, "generate_images_litellm", boom)

    result = run_orchestrator(
        AskRequest(question="draw me a diagram of the flow", mode=Mode.smart)
    )
    assert result.images is None


# --- the turn is told what is actually happening to it ------------------------


def _seen_question(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    def fake_call_model(**kwargs):
        seen["question"] = kwargs["question"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    return seen


def test_a_turn_with_an_image_coming_is_told_so(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live: asked "Can you draw a cat sitting?" twice in one conversation,
    the app said "Yes — image generation is enabled here" and, on a
    regenerate, "I can't generate images." Both as fact, one necessarily
    wrong. self_describe's per-turn tool list could not help — it only rides
    a turn where self-description fires, and a request for a cat is not a
    capabilities question."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gemini/imagen-4.0-generate-001")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "generate_images_litellm", lambda *a, **k: [])
    seen = _seen_question(monkeypatch)

    run_orchestrator(
        AskRequest(question="Can you draw a cat sitting?", mode=Mode.smart)
    )

    assert "an image generator is running on this question" in seen["question"]
    # It must not describe what it cannot see — that is the false claim
    # app/image_claims.py exists to catch, invited one step earlier.
    assert "Do NOT describe what the image shows" in seen["question"]


def test_an_image_request_with_the_feature_off_is_told_it_is_a_setting(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "I can't generate images" reads as an incapacity. The truth is a
    switch, and the difference matters to a reader who can go and flip it."""
    monkeypatch.delenv("IMAGE_GENERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    run_orchestrator(
        AskRequest(question="Can you draw a cat sitting?", mode=Mode.smart)
    )

    assert "switched OFF in this deployment" in seen["question"]
    assert "IMAGE_GENERATION" in seen["question"]


def test_an_ordinary_question_is_told_nothing_about_images(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Costs tokens on every turn if it leaks past image requests."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.smart))

    assert "IMAGE GROUND TRUTH" not in seen["question"]


def test_a_diagram_request_is_not_promised_an_image_it_will_not_get(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The diagram preference sends this to code execution, so no image is
    coming — and the model must not be told one is."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    run_orchestrator(
        AskRequest(question="draw me a diagram of the flow", mode=Mode.smart)
    )

    assert "an image generator is running" not in seen["question"]


def test_a_hosted_tool_turn_is_told_it_holds_the_tool_not_that_one_is_running(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hosted OpenAI tool is OFFERED; the model decides. Saying "an image
    is being generated" there would be false in the other direction."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)  # gpt-image-1
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    # The default resolved model is OpenAI-served, so the tool is attached.
    run_orchestrator(AskRequest(question="draw me a cat sitting", mode=Mode.smart))

    assert "you have an image_generation tool available" in seen["question"]
    assert "an image generator is running" not in seen["question"]


def test_a_diagram_turn_with_both_tools_is_pointed_at_code(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T15's lesson, applied where the MODEL is the one choosing: with both
    instruments in hand and a structure to draw, code is the right one."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("CODE_EXECUTION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    run_orchestrator(
        AskRequest(question="draw me a diagram of the flow", mode=Mode.smart)
    )

    assert "Build it with code" in seen["question"]
    assert "an image generator is running" not in seen["question"]


@pytest.mark.parametrize(
    "question",
    [
        # LIVE miss: \bdraw\b cannot see the verb inside "redraw", so this got
        # no image, no ground-truth block, and a guessed (false) "image
        # generation is switched off" answer.
        "Can you redraw yourself using similar looking logo's of the companies in your make up?",
        "redraw me as a cartoon",
        "re-draw the cat with a hat on",
    ],
)
def test_redraw_is_a_picture_verb(question: str) -> None:
    assert _looks_like_image_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # The electoral idiom "redraw" drags in with it.
        "redraw the district boundaries after the census",
        "redraw the boundaries of the map",
        "redraw the lines between the two teams",
    ],
)
def test_redraw_s_political_idiom_stays_out(question: str) -> None:
    assert _looks_like_image_request(question) is False
