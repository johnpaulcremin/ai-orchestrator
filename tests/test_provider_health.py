"""The provider circuit breaker (app/provider_health.py) and its two uses in
the orchestrator: an unhealthy primary is skipped before it can time out, and
an unhealthy fallback candidate is tried last rather than first.

The failure these pin is the one seen live: Ollama unreachable, so every
budget-tier question spent the full connect timeout failing before falling
back to a PAID model. Nothing remembered between requests that the model had
just failed, so request 50 paid the same penalty as request 1.
"""

from __future__ import annotations

import httpx
import pytest
from openai import APIConnectionError

import app.orchestrator as orchestrator
from app import provider_health
from app.fallback_reason import ALL_REASONS, CONNECTION_ERROR, TIMEOUT
from app.orchestrator import run_orchestrator
from app.schemas import AskRequest, Mode

DEAD = "ollama/llama3.1:8b"


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    """Process-local state: a breaker left open by one test would silently
    re-route another's request."""
    provider_health.reset()
    yield
    provider_health.reset()


def _trip(model: str = DEAD, reason: str = CONNECTION_ERROR) -> None:
    """Fail `model` exactly enough times to open its breaker ONCE.

    Not "several times": the counter resets on each trip, so an over-eager
    helper trips it twice and the cooldown assertions then measure the
    doubled, second-trip value.
    """
    for _ in range(provider_health._threshold()):
        provider_health.record_failure(model, reason)


def _unreachable() -> APIConnectionError:
    """What a dead local server actually raises through LiteLLM/the SDK —
    NOT Python's builtin ConnectionError, which classify_error_reason files
    under provider_error and which would therefore never trip the breaker."""
    return APIConnectionError(request=httpx.Request("POST", "http://localhost:11434"))


# --- what trips it, and what deliberately does not ---------------------------------


def test_repeated_connection_errors_open_the_breaker() -> None:
    assert provider_health.is_unhealthy(DEAD) is False
    provider_health.record_failure(DEAD, CONNECTION_ERROR)
    # One failure is ordinary noise; the default threshold is two.
    assert provider_health.is_unhealthy(DEAD) is False
    provider_health.record_failure(DEAD, CONNECTION_ERROR)
    assert provider_health.is_unhealthy(DEAD) is True


def test_timeouts_trip_it_too() -> None:
    _trip(reason=TIMEOUT)
    assert provider_health.is_unhealthy(DEAD) is True


@pytest.mark.parametrize(
    "reason", sorted(set(ALL_REASONS) - provider_health.TRIPPING_REASONS)
)
def test_non_reachability_failures_never_trip_it(reason: str) -> None:
    """Derived from ALL_REASONS, so a reason added later must be classified
    deliberately rather than silently inheriting "trips the breaker".

    A throttled free model already has free_tier's own daily cooldown; a
    provider error is often request-specific and would take a working
    provider out of service; context-length and unsupported-tool failures are
    properties of the request, not of reachability."""
    for _ in range(10):
        provider_health.record_failure(DEAD, reason)
    assert provider_health.is_unhealthy(DEAD) is False


def test_success_closes_the_breaker() -> None:
    _trip()
    assert provider_health.is_unhealthy(DEAD) is True
    provider_health.record_success(DEAD)
    assert provider_health.is_unhealthy(DEAD) is False


def test_a_recovered_model_starts_from_the_base_cooldown_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trip count resets on success, so an old outage's backoff is not
    inherited by an unrelated later one."""
    monkeypatch.setenv("PROVIDER_HEALTH_COOLDOWN", "10")
    _trip()
    first = provider_health.snapshot()[DEAD]
    provider_health.record_success(DEAD)
    _trip()
    second = provider_health.snapshot()[DEAD]
    # Both are the base cooldown (±20% jitter), not base then base*2.
    assert first <= 12.1
    assert second <= 12.1


def test_the_cooldown_grows_and_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROVIDER_HEALTH_COOLDOWN", "30")
    # Jitter is skipped at trips <= 0; assert the curve on the seeded values.
    assert provider_health.cooldown_for(0) == 0.0
    for trips, expected in [(1, 30), (2, 60), (3, 120)]:
        value = provider_health.cooldown_for(trips)
        assert expected * 0.8 <= value <= expected * 1.2
    # Capped: the 20th trip is not eight days long.
    assert provider_health.cooldown_for(20) <= 300 * 1.2


def test_the_breaker_expires_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cooldown that never expired would strand a recovered model."""
    monkeypatch.setenv("PROVIDER_HEALTH_COOLDOWN", "30")
    _trip()
    assert provider_health.is_unhealthy(DEAD) is True
    real_monotonic = provider_health.time.monotonic
    monkeypatch.setattr(
        provider_health.time, "monotonic", lambda: real_monotonic() + 3600
    )
    assert provider_health.is_unhealthy(DEAD) is False


def test_disabled_by_env_reports_everything_healthy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PROVIDER_HEALTH=false must restore exactly the pre-breaker behaviour."""
    _trip()
    assert provider_health.is_unhealthy(DEAD) is True
    monkeypatch.setenv("PROVIDER_HEALTH", "false")
    assert provider_health.is_unhealthy(DEAD) is False
    assert provider_health.healthy_first([DEAD, "gpt-5"]) == [DEAD, "gpt-5"]


# --- ordering, never dropping ------------------------------------------------------


def test_unhealthy_candidates_are_tried_last_not_dropped() -> None:
    _trip()
    assert provider_health.healthy_first([DEAD, "gpt-5"]) == ["gpt-5", DEAD]


def test_an_all_unhealthy_list_is_returned_intact() -> None:
    """A model whose breaker is open might still answer. Returning nothing
    would turn a degraded request into a failed one."""
    _trip(DEAD)
    _trip("gpt-5")
    assert provider_health.healthy_first([DEAD, "gpt-5"]) == [DEAD, "gpt-5"]


def test_snapshot_omits_expired_breakers(monkeypatch: pytest.MonkeyPatch) -> None:
    _trip()
    assert DEAD in provider_health.snapshot()
    real_monotonic = provider_health.time.monotonic
    monkeypatch.setattr(
        provider_health.time, "monotonic", lambda: real_monotonic() + 3600
    )
    assert provider_health.snapshot() == {}


# --- the orchestrator actually uses it ---------------------------------------------


def test_an_unhealthy_primary_is_skipped_before_it_can_time_out(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    """The whole point: the dead model is never dispatched, so the request
    does not wait out its timeout only to fall back to the same model this
    picks immediately."""
    monkeypatch.setenv("OPENAI_MODEL_FAST", DEAD)
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    called: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        model = str(kwargs["model"])
        called.append(model)
        if model == DEAD:
            raise _unreachable()
        return "answered by the fallback"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    _trip()

    result = run_orchestrator(AskRequest(question="hello", mode=Mode.fast))

    assert called == ["gpt-5"]  # the dead model was never tried
    assert result.answer == "answered by the fallback"
    assert result.model == "gpt-5"
    assert "provider health" in result.notes


def test_a_healthy_primary_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL_FAST", DEAD)
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    called: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kw: (called.append(str(kw["model"])), "ok")[1],
    )

    result = run_orchestrator(AskRequest(question="hello", mode=Mode.fast))

    assert called == [DEAD]
    assert "provider health" not in result.notes


def test_a_forced_model_is_never_substituted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller named an exact model. Answering on a different one because
    this process saw two timeouts would substitute our judgement for theirs."""
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    called: list[str] = []

    def fake_call_model(**kwargs: object) -> str:
        called.append(str(kwargs["model"]))
        return "ok"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)
    _trip()

    run_orchestrator(AskRequest(question="hello", mode=Mode.fast, model=DEAD))

    assert called[0] == DEAD


def test_a_live_connection_failure_opens_the_breaker_through_the_orchestrator(
    monkeypatch: pytest.MonkeyPatch, db_path
) -> None:
    """End to end: two ordinary failing requests are enough for the third to
    skip the dead model."""
    monkeypatch.setenv("OPENAI_MODEL_FAST", DEAD)
    monkeypatch.setenv("OPENAI_MODEL_FALLBACK", "gpt-5")
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())

    def fake_call_model(**kwargs: object) -> str:
        if str(kwargs["model"]) == DEAD:
            raise _unreachable()
        return "fallback answer"

    monkeypatch.setattr(orchestrator, "_call_model", fake_call_model)

    for _ in range(2):
        run_orchestrator(AskRequest(question="hello", mode=Mode.fast))

    assert provider_health.is_unhealthy(DEAD) is True


def test_with_no_healthy_alternative_the_primary_still_runs_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The breaker is an optimisation, not a veto: when every candidate looks
    unhealthy the original model is still dispatched — and answering closes
    its breaker, so one recovery is enough to stop the skipping.

    Every tier is pointed at DEAD because _fallback_models falls through to
    OPENAI_MODEL_FAST and then OPENAI_MODEL: merely unsetting
    OPENAI_MODEL_FALLBACK still leaves gpt-5 as a healthy candidate, which is
    what an earlier version of this test got wrong.
    """
    for tier in ("OPENAI_MODEL", "OPENAI_MODEL_FAST", "OPENAI_MODEL_FALLBACK"):
        monkeypatch.setenv(tier, DEAD)
    monkeypatch.setattr(orchestrator, "get_client", lambda: object())
    called: list[str] = []
    monkeypatch.setattr(
        orchestrator,
        "_call_model",
        lambda **kw: (called.append(str(kw["model"])), "ok")[1],
    )
    _trip()
    assert provider_health.is_unhealthy(DEAD) is True

    result = run_orchestrator(AskRequest(question="hello", mode=Mode.fast))

    assert called == [DEAD]
    assert result.answer == "ok"
    assert provider_health.is_unhealthy(DEAD) is False
