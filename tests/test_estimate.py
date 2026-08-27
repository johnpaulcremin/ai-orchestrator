"""Live token/cost preview (POST /v1/estimate): what a question would cost if
sent, computed from the SAME worst-case estimate the DAILY_BUDGET_USD gate
uses on dispatch (see budget.estimate_worst_case) — without ever spending a
model or classifier call.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.budget import estimate_worst_case
from app.orchestrator import code_execution_available_to
from app.orchestrator_tools import (
    _worst_case_image_cost,
    _worst_case_video_cost,
    image_wanted_flags_for,
    standalone_video_wanted_for,
)


def _estimate(client: TestClient, question: str, mode: str = "auto"):
    return client.post("/v1/estimate", json={"question": question, "mode": mode})


# --- budget.estimate_worst_case: unit behavior --------------------------------


def test_estimate_worst_case_prices_input_and_max_output() -> None:
    tokens, cost = estimate_worst_case("gpt-5", 800, "a" * 400)
    assert tokens == 100  # 400 chars // 4
    assert cost is not None
    assert cost > 0


def test_estimate_worst_case_none_for_unpriced_model() -> None:
    tokens, cost = estimate_worst_case("some-totally-unknown-model", 800, "hi")
    assert tokens == 0
    assert cost is None


# --- HTTP: POST /v1/estimate ----------------------------------------------------


def test_estimate_fast_mode_resolves_fast_model(client: TestClient) -> None:
    res = _estimate(client, "hi", mode="fast")
    assert res.status_code == 200
    body = res.json()
    assert body["model"]
    assert body["mode_used"] == "fast"
    assert body["input_tokens_estimate"] >= 0
    assert body["output_tokens_estimate"] > 0


def test_estimate_smart_mode_resolves_smart_model(client: TestClient) -> None:
    res = _estimate(client, "hi", mode="smart")
    assert res.status_code == 200
    assert res.json()["mode_used"] == "smart"


def test_estimate_never_calls_the_classifier(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto mode must resolve via the free heuristic fallback, never the paid
    classifier — decide_route is called with client=None specifically so it
    can never reach that branch, but assert it directly too."""

    def boom(*args, **kwargs):
        raise AssertionError("estimate must never call the AI classifier")

    # _classify_with_ai is only ever reached when decide_route's `client` arg
    # is not None; patch it to blow up if that ever happens.
    import app.routing as routing_module

    monkeypatch.setattr(routing_module, "_classify_with_ai", boom)

    res = _estimate(
        client, "please write a detailed essay about something", mode="auto"
    )
    assert res.status_code == 200


def test_estimate_larger_question_yields_larger_or_equal_estimate(
    client: TestClient,
) -> None:
    short = _estimate(client, "hi", mode="fast").json()
    long = _estimate(client, "explain " * 200, mode="fast").json()
    assert long["input_tokens_estimate"] >= short["input_tokens_estimate"]


def test_estimate_rejects_empty_question(client: TestClient) -> None:
    res = _estimate(client, "")
    assert res.status_code == 422


def test_estimate_does_not_persist_anything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview call must never touch add_message/conversations — it's a
    stateless, read-only computation."""
    import app.routers.messages as main_module

    def boom(*args, **kwargs):
        raise AssertionError("estimate must never persist a message")

    monkeypatch.setattr(main_module, "add_message", boom)
    res = _estimate(client, "hi")
    assert res.status_code == 200


# --- workflow mode: previews the reserve_workflow() ceiling, not a plan --------


def test_estimate_workflow_mode_resolves_smart_model(client: TestClient) -> None:
    res = _estimate(client, "do a multi-part task", mode="workflow")
    assert res.status_code == 200
    body = res.json()
    assert body["model"]
    assert body["mode_used"].startswith("workflow(up to ")
    assert body["output_tokens_estimate"] > 0


def test_estimate_workflow_mode_never_calls_the_planner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.workflow as workflow_module

    def boom(*args, **kwargs):
        raise AssertionError("estimate must never call the workflow planner")

    monkeypatch.setattr(workflow_module, "_plan_workflow", boom)
    res = _estimate(client, "do a multi-part task", mode="workflow")
    assert res.status_code == 200


def test_estimate_workflow_mode_scales_with_max_steps(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WORKFLOW_MAX_STEPS", "2")
    small = _estimate(client, "hi", mode="workflow").json()
    monkeypatch.setenv("WORKFLOW_MAX_STEPS", "6")
    large = _estimate(client, "hi", mode="workflow").json()
    assert large["output_tokens_estimate"] > small["output_tokens_estimate"]


def test_estimate_workflow_mode_exceeds_a_single_smart_call(
    client: TestClient,
) -> None:
    """The whole-workflow ceiling should genuinely be higher than a single
    smart-tier call's own estimate, since it prices multiple steps."""
    workflow_est = _estimate(client, "do a task", mode="workflow").json()
    smart_est = _estimate(client, "do a task", mode="smart").json()
    assert workflow_est["output_tokens_estimate"] > smart_est["output_tokens_estimate"]


# --- video clip cost in the preview -------------------------------------------
#
# The gap these close: /v1/estimate priced tokens only, so a question that was
# about to spend a dollar-scale clip previewed as a few cents. DAILY_BUDGET_USD
# was the only thing that saw the real figure, and only at dispatch.


def test_estimate_worst_case_adds_extra_cost() -> None:
    _, without = estimate_worst_case("gpt-5", 800, "hi")
    _, with_extra = estimate_worst_case("gpt-5", 800, "hi", extra_cost_usd=1.5)
    assert without is not None and with_extra is not None
    assert with_extra == pytest.approx(without + 1.5)


def test_estimate_worst_case_projects_extra_cost_for_an_unpriced_model() -> None:
    """Mirrors reserve(): an unknown TOKEN cost must not collapse a KNOWN
    artefact cost to None. The clip is real money whether or not the model
    that writes the prompt happens to be in the price table."""
    _, cost = estimate_worst_case("some-totally-unknown-model", 800, "hi", 1.5)
    assert cost == pytest.approx(1.5)


def test_estimate_worst_case_unpriced_and_no_extra_is_still_none() -> None:
    _, cost = estimate_worst_case("some-totally-unknown-model", 800, "hi", 0.0)
    assert cost is None


def test_estimate_prices_a_video_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    body = _estimate(client, "make a video of a cat playing piano").json()
    assert body["video_cost_usd_estimate"] is not None
    assert body["video_cost_usd_estimate"] > 0
    # The clip is a COMPONENT of the total, never an addition to it.
    assert body["cost_usd_estimate"] >= body["video_cost_usd_estimate"]


def test_estimate_video_cost_is_null_when_the_flag_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "false")
    body = _estimate(client, "make a video of a cat playing piano").json()
    assert body["video_cost_usd_estimate"] is None


def test_estimate_video_cost_is_null_for_an_ordinary_question(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    body = _estimate(client, "what is the capital of France").json()
    assert body["video_cost_usd_estimate"] is None


def test_estimate_video_dominates_the_token_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason this had to be surfaced separately rather than folded
    silently into one number: the clip is not a rounding adjustment, it is
    almost the entire figure."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    video = _estimate(client, "make a video of a cat playing piano").json()
    plain = _estimate(client, "write a poem about a cat playing piano").json()
    assert video["cost_usd_estimate"] > (plain["cost_usd_estimate"] or 0) * 10


def test_estimate_video_request_matches_what_dispatch_would_reserve(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint's contract is that the preview equals the gate. Assert it
    against the same helpers dispatch calls, not against a copied constant."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    question = "make a video of a cat playing piano"
    expected = _worst_case_video_cost(standalone_video_wanted_for(question))
    body = _estimate(client, question).json()
    assert body["video_cost_usd_estimate"] == pytest.approx(expected)


def test_estimate_workflow_mode_prices_a_clip_per_step(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow can produce one clip per step, so its ceiling scales with the
    step count — deliberately above reserve_workflow's token-only placeholder,
    which holds the money differently than this number answers the question
    'what could this cost me'."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    question = "make a video of a cat playing piano"
    single = _worst_case_video_cost(standalone_video_wanted_for(question))
    body = _estimate(client, question, mode="workflow").json()
    assert body["video_cost_usd_estimate"] > single


def test_estimate_video_preview_spends_no_model_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pricing a clip must not make the preview billable. The video gate is a
    pure function of the flag and the question — no model, no classifier."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    called = False

    def _boom(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("the preview must never generate a video")

    monkeypatch.setattr("app.video_generation.generate_video", _boom)
    res = _estimate(client, "make a video of a cat playing piano")
    assert res.status_code == 200
    assert called is False


# --- image cost in the preview ------------------------------------------------
#
# Same gap as video, with one extra wrinkle: an image cost does NOT imply an
# image is coming. The hosted OpenAI tool is only OFFERED, so dispatch reserves
# for it on every question an OpenAI model answers — which the preview must
# match, and the UI must word differently.


def test_estimate_prices_an_image_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    body = _estimate(client, "draw me a cat wearing a hat").json()
    assert body["image_cost_usd_estimate"] is not None
    assert body["image_cost_usd_estimate"] > 0
    assert body["cost_usd_estimate"] >= body["image_cost_usd_estimate"]


def test_estimate_image_cost_is_null_when_the_flag_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "false")
    body = _estimate(client, "draw me a cat wearing a hat").json()
    assert body["image_cost_usd_estimate"] is None
    assert body["image_is_certain"] is False


def test_estimate_reserves_for_the_offered_tool_on_any_question(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing asymmetry with video. Under an OpenAI model with the
    OpenAI backend the hosted tool is offered on EVERY turn, so dispatch holds
    an image's budget even for a question that plainly wants no picture. The
    preview matches that, and flags it as NOT certain so the UI can hedge."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    body = _estimate(client, "what is the capital of France").json()
    assert body["image_cost_usd_estimate"] is not None
    assert body["image_is_certain"] is False


def test_estimate_image_matches_what_dispatch_would_reserve(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assert against the same shared gate dispatch calls, not a copied number
    — the point of extracting image_wanted_flags_for was that a second copy of
    this condition is how a preview starts lying."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    question = "draw me a cat wearing a hat"
    body = _estimate(client, question).json()
    model = body["model"]
    expected = _worst_case_image_cost(
        *image_wanted_flags_for(model, question, code_execution_available_to(model))
    )
    assert body["image_cost_usd_estimate"] == pytest.approx(expected)


def test_estimate_image_is_certain_on_a_non_openai_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the answering model is NOT OpenAI the hosted tool cannot be
    offered, so the only route to a picture is the standalone call this app
    makes itself — which fires on the phrase heuristic and therefore IS
    certain. Same money, opposite confidence."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    monkeypatch.setenv("OPENAI_MODEL_FAST", "claude-sonnet-5")
    body = _estimate(client, "draw me a cat wearing a hat", mode="smart").json()
    assert body["image_is_certain"] is True
    assert body["image_cost_usd_estimate"] is not None


def test_estimate_ordinary_question_on_a_non_openai_model_has_no_image_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("OPENAI_MODEL_SMART", "claude-sonnet-5")
    body = _estimate(client, "what is the capital of France", mode="smart").json()
    assert body["image_cost_usd_estimate"] is None


def test_estimate_counts_image_and_video_together(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch sums both when both are in play (the video veto only blocks the
    STANDALONE image path, not the offered hosted tool), so the preview does
    too — and the total must cover both, not just the larger."""
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    body = _estimate(client, "make a video of a cat playing piano").json()
    video = body["video_cost_usd_estimate"]
    image = body["image_cost_usd_estimate"]
    assert video is not None and image is not None
    assert body["cost_usd_estimate"] >= video + image
