"""Zero-cost local image generation (app/local_images.py): the standalone
image call routed to a LOCAL_ENDPOINTS Stable Diffusion server speaking the
AUTOMATIC1111 /sdapi/v1/txt2img API, priced $0, never raising.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app import local_images
from app.local_images import (
    generate_images_local,
    parse_size,
    request_body,
    timeout_seconds,
    txt2img_url,
)


def _fake_response(payload: dict, status: int = 200):
    def raise_for_status() -> None:
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

    return SimpleNamespace(json=lambda: payload, raise_for_status=raise_for_status)


# --- URL shape --------------------------------------------------------------


def test_txt2img_url_strips_the_documented_v1_suffix() -> None:
    """LOCAL_ENDPOINTS values are OpenAI-compatible bases and the docs show
    them ending in /v1. A1111's API is NOT under /v1, so copying that form
    must not produce /v1/sdapi/v1/txt2img (a 404 the operator would never
    connect to the /v1 they were told to write)."""
    assert (
        txt2img_url("http://localhost:7860/v1")
        == "http://localhost:7860/sdapi/v1/txt2img"
    )
    assert (
        txt2img_url("http://localhost:7860/v1/")
        == "http://localhost:7860/sdapi/v1/txt2img"
    )
    assert (
        txt2img_url("http://localhost:7860") == "http://localhost:7860/sdapi/v1/txt2img"
    )
    assert (
        txt2img_url("http://localhost:7860/")
        == "http://localhost:7860/sdapi/v1/txt2img"
    )


def test_txt2img_url_v1_suffix_is_case_insensitive() -> None:
    assert txt2img_url("http://host:1/V1") == "http://host:1/sdapi/v1/txt2img"


# --- size ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1024x1024", (1024, 1024)),
        ("1024x1536", (1024, 1536)),
        ("512X768", (512, 768)),
        (" 640 x 480 ", (640, 480)),
        ("768×768", (768, 768)),
    ],
)
def test_parse_size_reads_width_by_height(raw: str, expected: tuple[int, int]) -> None:
    assert parse_size(raw) == expected


@pytest.mark.parametrize("raw", ["auto", "", "banana", "0x0", "1024", "x1024"])
def test_parse_size_falls_back_to_the_square_default(raw: str) -> None:
    """A1111 has no "auto". A bad IMAGE_GENERATION_SIZE must not be what makes
    images fail, so anything unparseable renders at the default instead."""
    assert parse_size(raw) == (1024, 1024)


# --- timeout ------------------------------------------------------------------


def test_timeout_defaults_generously(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCAL_IMAGE_TIMEOUT", raising=False)
    assert timeout_seconds() == 300.0


def test_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_IMAGE_TIMEOUT", "45")
    assert timeout_seconds() == 45.0


@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_timeout_rejects_non_positive_and_garbage(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Zero means "no timeout" to some HTTP clients — the opposite of what an
    operator typing 0 meant — so it falls back rather than passing through."""
    monkeypatch.setenv("LOCAL_IMAGE_TIMEOUT", raw)
    assert timeout_seconds() == 300.0


# --- request body ---------------------------------------------------------------


def test_request_body_selects_the_checkpoint_and_restores_it() -> None:
    body = request_body("sd_xl_base_1.0", "a cat", "1024x1536")
    assert body["prompt"] == "a cat"
    assert (body["width"], body["height"]) == (1024, 1536)
    assert body["batch_size"] == 1 and body["n_iter"] == 1
    assert body["override_settings"] == {"sd_model_checkpoint": "sd_xl_base_1.0"}
    # A shared server must not be left repointed for the next user.
    assert body["override_settings_restore_afterwards"] is True


def test_request_body_default_checkpoint_sends_no_override() -> None:
    """The literal `default` means "whatever the server has loaded" — the
    common single-model case, where naming the file only invites a typo."""
    body = request_body("default", "a cat", "auto")
    assert "override_settings" not in body
    assert "override_settings_restore_afterwards" not in body


def test_request_body_default_is_case_insensitive() -> None:
    assert "override_settings" not in request_body("DEFAULT", "x", "auto")


# --- generate_images_local ------------------------------------------------------


def test_generate_returns_data_urls_from_bare_base64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860/v1"})
    )
    seen: dict = {}

    def fake_post(url, *, json, timeout):
        seen.update(url=url, body=json, timeout=timeout)
        return _fake_response({"images": ["aaa", "bbb"]})

    monkeypatch.setattr(local_images.httpx, "post", fake_post)

    images = generate_images_local("local:sd/sd_xl_base_1.0", "a cat", "1024x1024")

    assert images == ["data:image/png;base64,aaa", "data:image/png;base64,bbb"]
    assert seen["url"] == "http://localhost:7860/sdapi/v1/txt2img"
    assert seen["body"]["prompt"] == "a cat"
    assert seen["body"]["override_settings"] == {
        "sd_model_checkpoint": "sd_xl_base_1.0"
    }
    assert seen["timeout"] == 300.0


def test_generate_accepts_an_already_prefixed_data_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some forks return a data: header already; it must not be double-wrapped
    into data:image/png;base64,data:image/png;base64,..."""
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860"}))
    monkeypatch.setattr(
        local_images.httpx,
        "post",
        lambda *a, **k: _fake_response({"images": ["data:image/png;base64,zzz"]}),
    )
    assert generate_images_local("local:sd/default", "x", "auto") == [
        "data:image/png;base64,zzz"
    ]


def test_generate_unconfigured_name_returns_empty_without_calling_the_network(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A name missing from LOCAL_ENDPOINTS is a CONFIGURATION problem, logged
    as one and distinctly from an outage — the fixes are different."""
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"other": "http://x:1"}))

    def boom(*a, **k):
        raise AssertionError("no server was named; nothing must be called")

    monkeypatch.setattr(local_images.httpx, "post", boom)
    with caplog.at_level("WARNING"):
        assert generate_images_local("local:sd/default", "x", "auto") == []
    assert "images.local_unconfigured" in caplog.text
    assert "images.local_generate_failed" not in caplog.text


def test_generate_unset_local_endpoints_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCAL_ENDPOINTS", raising=False)
    monkeypatch.setattr(
        local_images.httpx, "post", lambda *a, **k: pytest.fail("must not be called")
    )
    assert generate_images_local("local:sd/default", "x", "auto") == []


def test_generate_unreachable_server_returns_empty_and_logs_the_outage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A dead local server is the EXPECTED failure (the operator has not
    started it yet). It must be the ordinary "image failed" path — an empty
    list and a log line — never an exception out of the request."""
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860"}))

    def refuse(*a, **k):
        raise ConnectionError("refused")

    monkeypatch.setattr(local_images.httpx, "post", refuse)
    with caplog.at_level("WARNING"):
        assert generate_images_local("local:sd/default", "x", "auto") == []
    assert "images.local_generate_failed" in caplog.text


def test_generate_http_error_status_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860"}))
    monkeypatch.setattr(
        local_images.httpx, "post", lambda *a, **k: _fake_response({}, status=500)
    )
    assert generate_images_local("local:sd/default", "x", "auto") == []


def test_generate_tolerates_a_malformed_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860"}))
    for payload in ({}, {"images": None}, {"images": [None, "", 3]}, []):
        monkeypatch.setattr(
            local_images.httpx, "post", lambda *a, _p=payload, **k: _fake_response(_p)
        )
        assert generate_images_local("local:sd/default", "x", "auto") == []


def test_generate_honours_the_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860"}))
    monkeypatch.setenv("LOCAL_IMAGE_TIMEOUT", "12")
    seen: dict = {}

    def fake_post(url, *, json, timeout):
        seen["timeout"] = timeout
        return _fake_response({"images": ["a"]})

    monkeypatch.setattr(local_images.httpx, "post", fake_post)
    generate_images_local("local:sd/default", "x", "auto")
    assert seen["timeout"] == 12.0


# --- the fork in providers.generate_images_litellm ------------------------------


def test_providers_routes_local_ids_to_the_local_backend_not_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trap this closes: provider_of("local:sd/x") is "litellm" on the
    strength of the slash alone, so without the fork the literal id reached
    litellm.image_generation, raised provider-not-found, and was swallowed as
    a generic failure that never touched the operator's server."""
    import app.providers as providers

    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"sd": "http://localhost:7860"}))
    monkeypatch.setattr(
        providers, "_litellm", lambda: pytest.fail("litellm must not be reached")
    )
    monkeypatch.setattr(
        local_images.httpx, "post", lambda *a, **k: _fake_response({"images": ["ok"]})
    )
    assert providers.generate_images_litellm(
        "local:sd/default", "a cat", "high", "auto"
    ) == ["data:image/png;base64,ok"]


def test_providers_still_uses_litellm_for_non_local_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.providers as providers

    fake = SimpleNamespace(
        image_generation=lambda **_kw: SimpleNamespace(
            data=[SimpleNamespace(b64_json="q")]
        )
    )
    monkeypatch.setattr(providers, "_litellm", lambda: fake)
    monkeypatch.setattr(
        local_images.httpx,
        "post",
        lambda *a, **k: pytest.fail("local path must not run"),
    )
    assert providers.generate_images_litellm(
        "fal_ai/flux", "a cat", "high", "auto"
    ) == ["data:image/png;base64,q"]


# --- pricing ------------------------------------------------------------------------


def test_local_image_is_priced_zero_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.usage import estimate_image_cost

    monkeypatch.delenv("IMAGE_GENERATION_COST_USD", raising=False)
    assert estimate_image_cost(1, "high", "local:sd/default") == 0.0
    assert estimate_image_cost(3, "high", "local:sd/default") == 0.0


def test_local_image_zero_is_zero_not_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.0 means "genuinely free"; None means "unpriced". The distinction is
    load-bearing for the budget gate, and a local image is the former."""
    from app.usage import estimate_image_cost

    monkeypatch.delenv("IMAGE_GENERATION_COST_USD", raising=False)
    assert estimate_image_cost(1, "high", "local:sd/default") is not None
    assert estimate_image_cost(0, "high", "local:sd/default") is None


def test_explicit_image_cost_env_wins_over_local_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors MODEL_PRICING beating a local text model's $0, for anyone
    accounting for their own hardware."""
    from app.usage import estimate_image_cost

    monkeypatch.setenv("IMAGE_GENERATION_COST_USD", "0.05")
    assert estimate_image_cost(2, "high", "local:sd/default") == pytest.approx(0.10)


def test_non_local_image_pricing_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.usage import estimate_image_cost

    monkeypatch.delenv("IMAGE_GENERATION_COST_USD", raising=False)
    assert estimate_image_cost(1, "high", "gpt-image-1") == pytest.approx(0.19)
    assert estimate_image_cost(1, "high") == pytest.approx(0.19)
