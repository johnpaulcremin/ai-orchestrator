"""Video generation: the flag/config helpers, the trigger heuristic (both
halves, and the modifier senses that must never buy a clip), the submit/poll/
download call, cost estimation, the never-cache invariant, orchestrator wiring,
and end-to-end persistence.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.orchestrator as orchestrator
import app.video_generation as video_generation
from app import cache
from app.database import add_message, create_conversation, list_messages
from app.orchestrator import run_orchestrator, stream_orchestrator
from app.schemas import AskRequest, Mode
from app.usage import estimate_video_cost
from app.video_generation import (
    generate_video,
    looks_like_video_request,
    video_generation_enabled,
    video_generation_model,
    video_generation_seconds,
    video_generation_size,
)


# --- config helpers -----------------------------------------------------------


def test_video_generation_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_GENERATION", raising=False)
    assert video_generation_enabled() is False
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    assert video_generation_enabled() is True
    monkeypatch.setenv("VIDEO_GENERATION", "false")
    assert video_generation_enabled() is False


def test_video_model_defaults_to_sora(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default needs no key this app doesn't already require: OPENAI_API_KEY
    is mandatory anyway, so flipping the flag is the only setup step."""
    monkeypatch.delenv("VIDEO_GENERATION_MODEL", raising=False)
    assert video_generation_model() == "sora-2"
    monkeypatch.setenv("VIDEO_GENERATION_MODEL", "gemini/veo-3.0-generate-001")
    assert video_generation_model() == "gemini/veo-3.0-generate-001"


def test_video_seconds_and_size_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_GENERATION_SECONDS", raising=False)
    monkeypatch.delenv("VIDEO_GENERATION_SIZE", raising=False)
    assert video_generation_seconds() == "4"
    assert video_generation_size() == "720x1280"
    monkeypatch.setenv("VIDEO_GENERATION_SECONDS", "8")
    monkeypatch.setenv("VIDEO_GENERATION_SIZE", "1280x720")
    assert video_generation_seconds() == "8"
    assert video_generation_size() == "1280x720"


def test_a_video_request_vetoes_the_image_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The two heuristics are independent and their vocabularies overlap, so a
    request naming ONE artefact matched both and was billed for both — the
    video's price plus the image's. Video is the more specific reading: nobody
    writes "make a video of X" wanting a still."""
    from app.orchestrator import _tool_flags_for

    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    for question in (
        "generate a video of my avatar",
        "make a clip of the illustration",
        "render a video of the mockup",
        "produce an animation of the logo",
    ):
        _, _, standalone_image, standalone_video, *_ = _tool_flags_for(
            "claude-sonnet-5", AskRequest(question=question), False
        )
        assert standalone_video is True, question
        assert standalone_image is False, question


def test_an_ordinary_image_request_is_untouched_by_that_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the veto must narrow the image path only where a video is
    actually coming, not switch it off in general."""
    from app.orchestrator import _tool_flags_for

    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    _, _, standalone_image, standalone_video, *_ = _tool_flags_for(
        "claude-sonnet-5", AskRequest(question="draw me a cat"), False
    )
    assert standalone_image is True
    assert standalone_video is False


def test_timeout_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_GENERATION_TIMEOUT", raising=False)
    assert video_generation._timeout_seconds() == 180.0
    monkeypatch.setenv("VIDEO_GENERATION_TIMEOUT", "45")
    assert video_generation._timeout_seconds() == 45.0


@pytest.mark.parametrize("raw", ["nonsense", "0", "-5", ""])
def test_timeout_rejects_a_value_that_would_never_return(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero/negative/garbage ceiling would make the poll loop meaningless —
    and this loop blocks the user's HTTP request, so "no ceiling" is the one
    outcome that must not be reachable by typo."""
    monkeypatch.setenv("VIDEO_GENERATION_TIMEOUT", raw)
    assert video_generation._timeout_seconds() == 180.0


# --- the trigger: things that ARE a request to generate a video ---------------


@pytest.mark.parametrize(
    "question",
    [
        "make a video of a cat playing piano",
        "generate a video of waves at sunset",
        "create a short video about the product launch",
        "produce a video of a rocket taking off",
        "make me a clip of a dog running",
        "generate an animation of the water cycle",
        "render a movie of the city at night",
        "create a timelapse of a flower opening",
        "make a gif of a bouncing ball",
        "shoot a trailer for the app",
        "whip up a reel for the launch",
        # An adjective between article and noun, matching the image grammar's
        # allowance.
        "make a short looping video of rain",
        # The verb carrying the request on its own.
        "animate this logo",
        "animate a bouncing ball",
    ],
)
def test_looks_like_video_request_positive(question: str) -> None:
    assert looks_like_video_request(question) is True


# --- the trigger: the modifier senses, each a paid false positive -------------


@pytest.mark.parametrize(
    "question",
    [
        # "video" as a MODIFIER — overwhelmingly how the word is used, and the
        # reason the noun rule has to check what FOLLOWS it.
        "create a video game about space",
        "make a video call link for the standup",
        "generate a video player component in React",
        "show me a video tutorial on hooks",
        "produce a video essay outline",
        "render a video card benchmark table",
        "create a video streaming architecture diagram",
        "make a video codec comparison",
        "show me a video link for the docs",
        "create a video editing workflow",
        "generate a video transcript summary",
        "make a video meeting agenda",
    ],
)
def test_video_as_a_modifier_never_buys_a_clip(question: str) -> None:
    """Each of these reads as "make a video ..." to a naive substring check and
    is not a request for a rendered clip at all. At 10-100x the price of an
    image, one of these firing is a real amount of money."""
    assert looks_like_video_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        # Found by testing the ORIGINAL denylist version of this heuristic, and
        # the reason it is now an allowlist. Each fired because the word after
        # "video" simply was not on the list of disqualifying nouns — a shape
        # that can only ever be as complete as the last person's imagination.
        "how do I make a video load faster",
        "produce a report on video engagement",
        # Nothing at all followed "video" here: the hyphen means a word-based
        # check finds "checklist" two tokens away, or nothing.
        "make a video-editing checklist",
        "create a video/audio pipeline",
        # Same class, found while widening the trap set.
        "make a video streaming service",
        "generate a video sitemap for SEO",
        "produce a report on video quality metrics",
        "show me a video player component",
        # "show"/"give" were dropped from the maker verbs: for a moving picture
        # they far more often mean "find me one that exists" than "render one".
        # Losing these to keep the three below is the right side of the trade.
        "show me a video of a robot dancing",
        "Show me the trailer for Dune",
        "Can you show me a clip from that movie?",
        "Give me a movie about time travel to watch tonight",
    ],
)
def test_the_denylist_era_false_positives_stay_dead(question: str) -> None:
    """A regression guard with a specific history. The rule is now "the video
    noun must be the HEAD of the phrase", so an unanticipated next-word means
    "not a video request" (free, wrong once) rather than "generate a clip"
    (billed, wrong every time)."""
    assert looks_like_video_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        # The flip side of that inversion: an allowlist can only under-fire, so
        # the phrasings people actually use have to be pinned too.
        "make me a video",
        "make a video.",
        "generate a video please",
        "create a short video for the launch",
        "make a video with a dog in it",
        "produce a video that shows the flow",
        "generate a timelapse of a flower opening",
    ],
)
def test_the_head_position_rule_still_admits_real_requests(question: str) -> None:
    assert looks_like_video_request(question) is True


@pytest.mark.parametrize(
    "question",
    [
        # "animate" in its abstract sense, and its front-end sense — a CSS
        # animation is not a video.
        "animate the css transition on hover",
        "animate this component when it mounts",
        "animate the sidebar sliding in",
        "animate a chart of daily spend",
        "animate the loading spinner",
        "what animated the discussion so much",
        "animate the debate with a real example",
    ],
)
def test_the_abstract_and_css_senses_of_animate_stay_out(question: str) -> None:
    assert looks_like_video_request(question) is False


@pytest.mark.parametrize(
    "question",
    [
        "what is the current weather",
        "explain how photosynthesis works",
        "write a Python function to sort a list",
        "draw me a cat wearing a hat",
        "summarise this meeting recording",
    ],
)
def test_looks_like_video_request_negative(question: str) -> None:
    assert looks_like_video_request(question) is False


def test_the_video_and_image_heuristics_do_not_overlap() -> None:
    """A picture request must not also buy a clip, and vice versa — they are
    separate paid calls and a turn that fired both would pay twice."""
    from app.orchestrator_tools import _looks_like_image_request

    assert _looks_like_image_request("draw me a cat") is True
    assert looks_like_video_request("draw me a cat") is False
    assert looks_like_video_request("make a video of a cat") is True
    assert _looks_like_image_request("make a video of a cat") is False


# --- estimate_video_cost ------------------------------------------------------


def test_estimate_video_cost_zero_count_is_none() -> None:
    assert estimate_video_cost(0, "4") is None


def test_estimate_video_cost_scales_with_clip_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Priced per second, not per clip: a flat per-clip figure would misprice a
    12-second render by 3x against a 4-second one."""
    monkeypatch.delenv("VIDEO_GENERATION_COST_USD", raising=False)
    four = estimate_video_cost(1, "4")
    twelve = estimate_video_cost(1, "12")
    assert four is not None and twelve is not None
    assert twelve == pytest.approx(four * 3)


def test_estimate_video_cost_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_GENERATION_COST_USD", "0.10")
    assert estimate_video_cost(1, "4") == pytest.approx(0.40)


@pytest.mark.parametrize("seconds", ["not-a-number", "", "0", "-3"])
def test_estimate_video_cost_falls_back_on_an_unusable_length(
    seconds: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This figure gates a budget reservation made BEFORE the call. A garbage
    VIDEO_GENERATION_SECONDS must not price the reservation at zero."""
    monkeypatch.setenv("VIDEO_GENERATION_COST_USD", "0.10")
    assert estimate_video_cost(1, seconds) == pytest.approx(0.40)


def test_a_video_is_priced_far_above_an_image() -> None:
    """The premise the whole feature's cost handling rests on."""
    from app.usage import estimate_image_cost

    video = estimate_video_cost(1, "4")
    image = estimate_image_cost(1, "high")
    assert video is not None and image is not None
    assert video > image * 5


# --- generate_video: submit, poll, download -----------------------------------


def _fake_litellm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    statuses: list[str],
    content: bytes | None = b"\x00\x01mp4",
    job_id: str = "vid_1",
    fail_on: str = "",
) -> dict[str, object]:
    """A LiteLLM double whose job reaches `statuses` in order across polls.

    The last status REPEATS once the list runs out, so a test can model a render
    that never finishes. An earlier version popped from a finite list, which
    meant "polls forever" actually ended in an IndexError that the status-call
    except swallowed — the timeout test then asserted `[]` and got `[]` for
    entirely the wrong reason, and passed with the deadline logic deleted.

    `time.sleep` advances a fake monotonic clock rather than really sleeping, so
    the deadline is reached in zero wall-clock time and is genuinely what ends
    the loop.
    """
    seen: dict[str, object] = {"status_calls": 0, "kwargs": {}}
    remaining = list(statuses)

    def _next_status() -> str:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    def _submit(**kwargs):
        if fail_on == "generate":
            raise RuntimeError("boom")
        seen["kwargs"] = kwargs
        return SimpleNamespace(id=job_id, status=_next_status(), error=None)

    def _status(**kwargs):
        if fail_on == "status":
            raise RuntimeError("boom")
        seen["status_calls"] = int(seen["status_calls"]) + 1  # type: ignore[arg-type]
        seen["status_provider"] = kwargs.get("custom_llm_provider")
        seen["status_timeout"] = kwargs.get("timeout")
        return SimpleNamespace(id=job_id, status=_next_status(), error=None)

    def _content(**kwargs):
        if fail_on == "content":
            raise RuntimeError("boom")
        seen["content_provider"] = kwargs.get("custom_llm_provider")
        return content

    monkeypatch.setattr(
        video_generation,
        "_litellm",
        lambda: SimpleNamespace(
            video_generation=_submit,
            video_status=_status,
            video_content=_content,
        ),
    )
    # A fake clock: sleeping advances time instead of spending it, so a
    # deadline test finishes instantly AND actually exercises the deadline.
    clock = {"now": 0.0}
    monkeypatch.setattr(video_generation.time, "monotonic", lambda: clock["now"])

    def _sleep(seconds):
        clock["now"] += max(float(seconds), 0.001)

    monkeypatch.setattr(video_generation.time, "sleep", _sleep)
    return seen


def test_generate_video_returns_a_data_url_when_the_job_completes_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_litellm(monkeypatch, statuses=["completed"], content=b"MP4BYTES")
    videos = generate_video("a cat playing piano")
    assert videos == ["data:video/mp4;base64,TVA0QllURVM="]


def test_generate_video_polls_until_the_job_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A video is a JOB, not a value — the bytes only exist once the provider
    finishes rendering, which is the whole reason this module polls."""
    seen = _fake_litellm(
        monkeypatch, statuses=["queued", "in_progress", "completed"], content=b"OK"
    )
    videos = generate_video("a cat")
    assert videos == ["data:video/mp4;base64,T0s="]
    assert seen["status_calls"] == 2


def test_generate_video_passes_the_configured_length_and_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION_SECONDS", "8")
    monkeypatch.setenv("VIDEO_GENERATION_SIZE", "1280x720")
    monkeypatch.setenv("VIDEO_GENERATION_MODEL", "sora-2")
    seen = _fake_litellm(monkeypatch, statuses=["completed"])
    generate_video("a cat")
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["seconds"] == "8"
    assert kwargs["size"] == "1280x720"
    assert kwargs["model"] == "sora-2"


def test_generate_video_tells_the_status_call_which_provider_to_ask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The status/download calls take a bare job id, which carries no prefix to
    infer the provider from — so it has to be passed explicitly or a Veo job is
    looked up against OpenAI."""
    monkeypatch.setenv("VIDEO_GENERATION_MODEL", "gemini/veo-3.0-generate-001")
    seen = _fake_litellm(monkeypatch, statuses=["queued", "completed"])
    generate_video("a cat")
    assert seen["status_provider"] == "gemini"
    assert seen["content_provider"] == "gemini"


def test_generate_video_gives_up_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll blocks the user's request, so an endless render must end as
    prose-plus-an-honest-note rather than a request that never returns.

    The double repeats "queued" forever, so ONLY the deadline can end this. The
    control below pins that: same setup, generous timeout, and it completes.
    """
    monkeypatch.setenv("VIDEO_GENERATION_TIMEOUT", "10")
    seen = _fake_litellm(monkeypatch, statuses=["queued"])
    assert generate_video("a cat") == []
    # ~10s of deadline at a 2s poll interval — proof it looped and gave up,
    # rather than falling out of the loop some other way.
    assert 4 <= int(seen["status_calls"]) <= 6  # type: ignore[arg-type]


def test_the_timeout_is_what_ends_that_loop_and_not_something_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control for the test above: identical double, but the job completes
    on the second poll, so the same code path returns a video. Without this,
    that test would still pass if the poll loop were broken outright."""
    monkeypatch.setenv("VIDEO_GENERATION_TIMEOUT", "10")
    _fake_litellm(monkeypatch, statuses=["queued", "completed"], content=b"OK")
    assert generate_video("a cat") == ["data:video/mp4;base64,T0s="]


def test_every_provider_call_is_bounded_by_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The poll COUNT is not a wall-clock bound. LiteLLM defaults to 600s (and
    no bound at all on the download), so without an explicit timeout a single
    hung call holds the user's request far past the ceiling this setting
    documents."""
    monkeypatch.setenv("VIDEO_GENERATION_TIMEOUT", "30")
    seen = _fake_litellm(monkeypatch, statuses=["queued", "completed"], content=b"OK")
    generate_video("a cat")
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert 0 < float(kwargs["timeout"]) <= 30
    assert 0 < float(seen["status_timeout"]) <= 30  # type: ignore[arg-type]


@pytest.mark.parametrize("stage", ["generate", "status", "content"])
def test_generate_video_never_raises_on_a_provider_failure(
    stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A video is an enrichment on top of the text answer, not something worth
    failing the whole request over — same contract as generate_images_litellm."""
    _fake_litellm(monkeypatch, statuses=["queued", "completed"], fail_on=stage)
    assert generate_video("a cat") == []


def test_generate_video_returns_nothing_for_a_failed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_litellm(monkeypatch, statuses=["failed"])
    assert generate_video("a cat") == []


def test_generate_video_skips_an_oversized_clip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipped rather than truncated: half an MP4 is not a shorter video, it is
    a broken file that renders as a silent failure."""
    monkeypatch.setattr(video_generation, "_MAX_VIDEO_BYTES", 8)
    _fake_litellm(monkeypatch, statuses=["completed"], content=b"x" * 64)
    assert generate_video("a cat") == []


def test_generate_video_returns_nothing_for_an_empty_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom():
        raise AssertionError("must not reach the provider with an empty prompt")

    monkeypatch.setattr(video_generation, "_litellm", boom)
    assert generate_video("   ") == []


# --- orchestrator wiring ------------------------------------------------------


def test_run_orchestrator_generates_a_video_when_the_phrase_matches(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")
    monkeypatch.setattr(
        orchestrator, "generate_video", lambda *_a, **_k: ["data:video/mp4;base64,aaa"]
    )

    result = run_orchestrator(
        AskRequest(question="make a video of a cat", mode=Mode.smart)
    )
    assert result.videos == ["data:video/mp4;base64,aaa"]
    assert "Here you go." in result.answer


def test_run_orchestrator_skips_the_video_call_for_an_ordinary_question(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "42")

    def boom(*_a, **_k):
        raise AssertionError("must not spend on a video for an ordinary question")

    monkeypatch.setattr(orchestrator, "generate_video", boom)

    result = run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.smart))
    assert result.videos is None


def test_the_flag_being_off_means_no_call_at_all(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIDEO_GENERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")

    def boom(*_a, **_k):
        raise AssertionError("the feature is off")

    monkeypatch.setattr(orchestrator, "generate_video", boom)

    result = run_orchestrator(
        AskRequest(question="make a video of a cat", mode=Mode.smart)
    )
    assert result.videos is None


def test_the_video_path_is_independent_of_which_model_answers(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider hosts a video tool a chat model can call, so this is a
    standalone call — it must fire whoever the router picked."""
    from app.routing import RouteDecision

    monkeypatch.setenv("VIDEO_GENERATION", "true")
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
        orchestrator, "generate_video", lambda *_a, **_k: ["data:video/mp4;base64,aaa"]
    )

    result = run_orchestrator(
        AskRequest(question="make a video of a cat", mode=Mode.smart)
    )
    assert result.videos == ["data:video/mp4;base64,aaa"]


def test_run_orchestrator_says_so_when_the_video_call_returns_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """generate_video never raises — a refused key, a failed job and a timeout
    all come back as []. Saying nothing would leave the user asking where their
    video went and the model guessing an answer."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setenv("VIDEO_GENERATION_MODEL", "sora-2")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "Here you go.")
    monkeypatch.setattr(orchestrator, "generate_video", lambda *_a, **_k: [])

    result = run_orchestrator(
        AskRequest(question="make a video of a cat", mode=Mode.smart)
    )
    assert result.videos is None
    assert "couldn't be generated" in result.answer
    assert "sora-2" in result.answer


def test_run_orchestrator_populates_video_cost(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setenv("VIDEO_GENERATION_COST_USD", "0.10")
    monkeypatch.setenv("VIDEO_GENERATION_SECONDS", "4")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")
    monkeypatch.setattr(
        orchestrator, "generate_video", lambda *_a, **_k: ["data:video/mp4;base64,aaa"]
    )

    result = run_orchestrator(
        AskRequest(question="make a video of a cat", mode=Mode.smart)
    )
    assert result.cost_usd == pytest.approx(0.40)


def test_a_generated_video_is_never_cached(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESPONSE_CACHE", "true")
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_call_model", lambda **_kw: "ok")
    monkeypatch.setattr(
        orchestrator, "generate_video", lambda *_a, **_k: ["data:video/mp4;base64,aaa"]
    )

    question = "make a video of a cat"
    run_orchestrator(AskRequest(question=question, mode=Mode.smart))
    assert cache.get(cache.make_key(question, "smart")) is None


def test_stream_orchestrator_done_frame_carries_the_video(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_stream_model", lambda **_kw: iter(["Here you go."])
    )
    monkeypatch.setattr(
        orchestrator, "generate_video", lambda *_a, **_k: ["data:video/mp4;base64,aaa"]
    )

    events = list(
        stream_orchestrator(
            AskRequest(question="make a video of a cat", mode=Mode.smart)
        )
    )
    done = events[-1]
    assert done["event"] == "done"
    assert done["data"]["videos"] == ["data:video/mp4;base64,aaa"]


def test_stream_orchestrator_omits_videos_when_none(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "_stream_model", lambda **_kw: iter(["hi"]))

    events = list(stream_orchestrator(AskRequest(question="hi", mode=Mode.fast)))
    assert "videos" not in events[-1]["data"]


def test_stream_orchestrator_says_so_when_the_video_call_returns_nothing(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(
        orchestrator, "_stream_model", lambda **_kw: iter(["Here you go."])
    )
    monkeypatch.setattr(orchestrator, "generate_video", lambda *_a, **_k: [])

    events = list(
        stream_orchestrator(
            AskRequest(question="make a video of a cat", mode=Mode.smart)
        )
    )
    done = events[-1]
    assert "couldn't be generated" in done["data"]["answer"]


def test_the_fallback_path_reserves_the_video_cost_too(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary reservation is RELEASED when the primary call fails, so the
    fallback makes its own — and it was making it with a $0 video estimate while
    still firing the video call. With DAILY_BUDGET_USD near its cap that admits
    a request whose real cost is dollars, and the cap is sailed past.

    `standalone_video` has no model/provider dimension, so it is True on every
    fallback whenever it was True on the primary: this was not an edge case.
    """
    import app.budget as budget

    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setenv("VIDEO_GENERATION_COST_USD", "0.50")
    monkeypatch.setenv("VIDEO_GENERATION_SECONDS", "4")
    # A cross-provider candidate, so the chain has somewhere real to go.
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "claude-sonnet-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    reserved: list[float] = []
    real_reserve = budget.reserve

    def spy(model, max_output_tokens, question, extra_cost=0.0, owner=None, **kw):
        reserved.append(float(extra_cost))
        return real_reserve(
            model, max_output_tokens, question, extra_cost, owner=owner, **kw
        )

    monkeypatch.setattr(orchestrator.budget, "reserve", spy)

    calls = {"n": 0}

    def flaky(**_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("primary is down")
        return "Here you go."

    monkeypatch.setattr(orchestrator, "_call_model", flaky)
    monkeypatch.setattr(
        orchestrator, "generate_video", lambda *_a, **_k: ["data:video/mp4;base64,aaa"]
    )

    result = run_orchestrator(
        AskRequest(question="make a video of a cat", mode=Mode.smart)
    )

    assert result.videos == ["data:video/mp4;base64,aaa"], "the fallback did render one"
    assert len(reserved) >= 2, "primary and fallback each reserve"
    assert reserved[-1] == pytest.approx(2.0), (
        "the fallback reserved $0 for a clip it went on to generate"
    )


# --- the turn is told what is actually happening to it ------------------------


def _seen_question(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    def fake_call_model(**kwargs):
        seen["question"] = kwargs["question"]
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    return seen


def test_a_turn_with_a_video_coming_is_told_so(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image path learned, from a live contradiction inside one
    conversation, that a model told nothing about a call made alongside its own
    will confidently assert both that it can and that it cannot do the thing.
    Nothing about that was specific to images."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    monkeypatch.setattr(orchestrator, "generate_video", lambda *_a, **_k: [])
    seen = _seen_question(monkeypatch)

    run_orchestrator(AskRequest(question="make a video of a cat", mode=Mode.smart))

    assert "a video generator is running on this question" in seen["question"]
    assert "do NOT describe what the video shows" in seen["question"]


def test_a_video_request_with_the_feature_off_is_told_it_is_a_setting(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VIDEO_GENERATION", raising=False)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    run_orchestrator(AskRequest(question="make a video of a cat", mode=Mode.smart))

    assert "switched OFF in this deployment" in seen["question"]
    assert "VIDEO_GENERATION" in seen["question"]


def test_an_ordinary_question_is_told_nothing_about_video(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Costs tokens on every turn if it leaks past video requests."""
    monkeypatch.setenv("VIDEO_GENERATION", "true")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    seen = _seen_question(monkeypatch)

    run_orchestrator(AskRequest(question="what is 2+2", mode=Mode.smart))

    assert "VIDEO GROUND TRUTH" not in seen["question"]


# --- persistence --------------------------------------------------------------


def test_add_message_and_list_messages_roundtrip_videos(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="here you go",
        videos=json.dumps(["data:video/mp4;base64,aaa"]),
    )
    messages = list_messages(conv["id"])
    assert json.loads(messages[0]["videos"]) == ["data:video/mp4;base64,aaa"]


def test_add_message_without_videos_stores_null(db_path: Path) -> None:
    conv = create_conversation("t", None)
    add_message(conversation_id=conv["id"], role="assistant", content="hi")
    assert list_messages(conv["id"])[0]["videos"] is None


def _create(client: TestClient) -> int:
    return int(client.post("/v1/conversations", json={"title": "t"}).json()["id"])


def test_ask_persists_and_returns_the_video(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.schemas import AskResponse

    def fake_run(req, routing_question=None, owner=None, history="", **_kw):
        return AskResponse(
            answer="Here's the video.",
            mode_used="smart",
            notes="n",
            videos=["data:video/mp4;base64,aaa"],
        )

    monkeypatch.setattr("app.routers.messages.run_orchestrator", fake_run)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask", json={"question": "make a video of a cat"}
    )
    assert r.status_code == 200
    assert r.json()["videos"] == ["data:video/mp4;base64,aaa"]

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["videos"] == ["data:video/mp4;base64,aaa"]


def test_stream_ask_persists_the_video_from_the_done_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_stream(req, routing_question=None, owner=None, history="", **_kw):
        yield {"event": "meta", "data": {"mode_used": "smart", "model": "m"}}
        yield {
            "event": "done",
            "data": {
                "answer": "Here's the video.",
                "mode_used": "smart",
                "notes": "n",
                "videos": ["data:video/mp4;base64,aaa"],
            },
        }

    monkeypatch.setattr("app.routers.messages.stream_orchestrator", fake_stream)

    cid = _create(client)
    r = client.post(
        f"/v1/conversations/{cid}/ask/stream",
        json={"question": "make a video of a cat"},
    )
    assert r.status_code == 200

    persisted = client.get(f"/v1/conversations/{cid}/messages").json()
    assistant = next(m for m in persisted if m["role"] == "assistant")
    assert assistant["videos"] == ["data:video/mp4;base64,aaa"]


def test_a_video_survives_duplicate_and_branch(
    client: TestClient, db_path: Path
) -> None:
    """Both copy loops hand-list every field, so a new one is dropped silently
    unless it is named in each."""
    conv = create_conversation("t", None)
    add_message(conversation_id=conv["id"], role="user", content="make a video")
    add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="here",
        videos=json.dumps(["data:video/mp4;base64,aaa"]),
    )

    dup = client.post(f"/v1/conversations/{conv['id']}/duplicate").json()
    copied = client.get(f"/v1/conversations/{dup['id']}/messages").json()
    assert next(m for m in copied if m["role"] == "assistant")["videos"] == [
        "data:video/mp4;base64,aaa"
    ]

    messages = list_messages(conv["id"])
    branch = client.post(
        f"/v1/conversations/{conv['id']}/messages/{messages[-1]['id']}/branch"
    ).json()
    branched = client.get(f"/v1/conversations/{branch['id']}/messages").json()
    assert next(m for m in branched if m["role"] == "assistant")["videos"] == [
        "data:video/mp4;base64,aaa"
    ]


def test_a_video_renders_on_a_public_share_link(
    client: TestClient, db_path: Path
) -> None:
    """The line SharedMessage draws is between the ANSWER and the private facts
    about how it was produced. A generated video is the answer."""
    conv = create_conversation("t", None)
    add_message(
        conversation_id=conv["id"],
        role="assistant",
        content="here",
        videos=json.dumps(["data:video/mp4;base64,aaa"]),
    )
    token = client.post(f"/v1/conversations/{conv['id']}/share", json={}).json()[
        "token"
    ]
    shared = client.get(f"/v1/shared/{token}").json()
    assert shared["messages"][0]["videos"] == ["data:video/mp4;base64,aaa"]


def test_an_imported_video_must_be_a_data_url(client: TestClient) -> None:
    """An import body is untrusted JSON and this value lands in a rendered
    <video src>. A bare URL would be an SSRF vector; a javascript: one worse."""
    body = {
        "title": "t",
        "messages": [
            {
                "role": "assistant",
                "content": "here",
                "videos": ["javascript:alert(1)"],
            }
        ],
    }
    assert client.post("/v1/conversations/import", json=body).status_code == 422


def test_a_valid_video_imports(client: TestClient) -> None:
    body = {
        "title": "t",
        "messages": [
            {
                "role": "assistant",
                "content": "here",
                "videos": ["data:video/mp4;base64,aaa"],
            }
        ],
    }
    r = client.post("/v1/conversations/import", json=body)
    assert r.status_code == 200
    messages = client.get(f"/v1/conversations/{r.json()['id']}/messages").json()
    assert messages[0]["videos"] == ["data:video/mp4;base64,aaa"]
