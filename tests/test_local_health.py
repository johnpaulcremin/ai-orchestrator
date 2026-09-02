"""An unreachable LOCAL model must not fail silently.

A local model has no API key, so the startup credential check can never flag
one — yet an unreachable local model is the more expensive misconfiguration:
it doesn't fail the request, it silently promotes every call on that tier to a
PAID fallback. The case this comes from: OLLAMA_API_BASE left at
http://host.docker.internal:11434 (correct inside a container, unresolvable
when the app runs natively) while Ollama itself was up and healthy on
localhost:11434 the whole time. The routing notes said so on every single
answer; nothing aggregated it, so a "free" budget tier billed gpt-5 prices.
"""

from __future__ import annotations

import logging
import socket
from typing import Any

import pytest

from app import local_health, main


# --- URL parsing / probing ----------------------------------------------------


def test_ollama_base_url_defaults_to_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    assert local_health.ollama_base_url() == "http://localhost:11434"


def test_ollama_base_url_honours_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_API_BASE", "http://192.168.1.5:11434")
    assert local_health.ollama_base_url() == "http://192.168.1.5:11434"


def test_blank_env_var_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_BASE", "   ")
    assert local_health.ollama_base_url() == "http://localhost:11434"


def test_reachable_when_something_is_listening() -> None:
    """Probe a real socket this test opens itself — no assumption about what
    happens to be running on the machine."""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert local_health.is_reachable(f"http://127.0.0.1:{port}") is True


def test_unreachable_when_nothing_is_listening() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # The socket is closed, so that port is now almost certainly free.
    assert local_health.is_reachable(f"http://127.0.0.1:{port}") is False


def test_unresolvable_host_is_unreachable_not_an_exception() -> None:
    """A DNS miss is the container-hostname case, and must read as
    "unreachable" rather than blowing up a startup check."""
    assert local_health.is_reachable("http://no-such-host.invalid:11434") is False


def test_malformed_url_is_unreachable() -> None:
    assert local_health.is_reachable("") is False
    assert local_health.is_reachable("::::") is False


def test_container_only_host_is_named() -> None:
    assert (
        local_health.container_only_host("http://host.docker.internal:11434")
        == "host.docker.internal"
    )
    assert local_health.container_only_host("http://localhost:11434") is None


# --- the startup warning ------------------------------------------------------


def _describe(models: list[str]) -> dict[str, Any]:
    return {
        "tiers": [
            {"effective_model": m, "key_env": "", "key_present": True} for m in models
        ],
        "categories": [],
    }


def test_warns_naming_the_docker_hostname_mixup(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The exact incident: the warning has to say WHICH fix to apply, since
    'connection failed' alone doesn't suggest 'you used the container name'."""
    monkeypatch.setattr(
        main, "describe_settings", lambda: _describe(["ollama/llama3.1:8b"])
    )
    monkeypatch.setenv("OLLAMA_API_BASE", "http://host.docker.internal:11434")
    monkeypatch.setattr(local_health, "is_reachable", lambda _url: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    message = caplog.text
    assert "startup.local_model_unreachable" in message
    assert "ollama/llama3.1:8b" in message
    assert "host.docker.internal" in message
    assert "localhost" in message
    assert "PAID" in message  # the consequence, stated in the warning itself


def test_warns_generically_when_the_server_is_simply_down(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        main, "describe_settings", lambda: _describe(["ollama/llama3.1:8b"])
    )
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")
    monkeypatch.setattr(local_health, "is_reachable", lambda _url: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert "is that server running?" in caplog.text
    assert "host.docker.internal" not in caplog.text


def test_silent_when_the_local_server_is_reachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        main, "describe_settings", lambda: _describe(["ollama/llama3.1:8b"])
    )
    monkeypatch.setattr(local_health, "is_reachable", lambda _url: True)

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert "local_model_unreachable" not in caplog.text


def test_never_probes_a_remote_model(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No startup network call for gpt-5/claude/gemini — this check is only
    ever about servers that are supposed to be on this machine."""
    monkeypatch.setattr(
        main,
        "describe_settings",
        lambda: _describe(["gpt-5", "claude-sonnet-5", "gemini/gemini-flash-latest"]),
    )
    probed: list[str] = []
    monkeypatch.setattr(
        local_health, "is_reachable", lambda url: probed.append(url) or False
    )

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert probed == []
    assert "local_model_unreachable" not in caplog.text


def test_probes_each_base_url_once_however_many_models_use_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Startup shouldn't pay one timeout per model when the tiers and half the
    categories all point at the same dead server."""
    monkeypatch.setattr(
        main,
        "describe_settings",
        lambda: _describe(["ollama/llama3.1:8b", "ollama_chat/llama3.1:8b"]),
    )
    probed: list[str] = []
    monkeypatch.setattr(
        local_health, "is_reachable", lambda url: probed.append(url) or False
    )

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert len(probed) == 1  # one base URL, one probe
    assert "ollama/llama3.1:8b" in caplog.text
    assert "ollama_chat/llama3.1:8b" in caplog.text


def test_covers_local_endpoint_models_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """LM Studio / vLLM / llama.cpp via LOCAL_ENDPOINTS have the same silent
    fallback-to-paid failure mode as Ollama."""
    monkeypatch.setattr(
        main, "describe_settings", lambda: _describe(["local:lmstudio/llama-3.1-8b"])
    )
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"lmstudio": "http://localhost:1234/v1"}')
    monkeypatch.setattr(local_health, "is_reachable", lambda _url: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert "local:lmstudio/llama-3.1-8b" in caplog.text
    assert "http://localhost:1234/v1" in caplog.text


# --- the image backend joins the probe ------------------------------------------
#
# IMAGE_GENERATION_MODEL is a flag's setting, not a tier or category, so the
# probe's walk over describe_settings() never saw it. A `local:` image server
# that was simply never started then looked exactly like a provider outage.


def test_probes_a_local_image_backend_when_the_flag_is_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(main, "describe_settings", lambda: _describe([]))
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "local:sd/default")
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"sd": "http://localhost:7860/v1"}')
    probed: list[str] = []

    def unreachable(url: str) -> bool:
        probed.append(url)
        return False

    monkeypatch.setattr(local_health, "is_reachable", unreachable)

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert probed == ["http://localhost:7860/v1"]
    assert "startup.local_model_unreachable" in caplog.text
    assert "local:sd/default" in caplog.text


def test_does_not_probe_the_image_backend_when_the_flag_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A configured-but-disabled backend is not a misconfiguration; warning
    about a server nothing will call would be noise on every boot."""
    monkeypatch.setattr(main, "describe_settings", lambda: _describe([]))
    monkeypatch.setenv("IMAGE_GENERATION", "false")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "local:sd/default")
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"sd": "http://localhost:7860/v1"}')
    monkeypatch.setattr(
        local_health, "is_reachable", lambda _url: pytest.fail("must not probe")
    )

    with caplog.at_level(logging.WARNING):
        main._warn_if_local_model_unreachable()

    assert "startup.local_model_unreachable" not in caplog.text


def test_a_non_local_image_backend_is_not_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe is for servers this process must reach on the local network.
    gpt-image-1 is a hosted API; nothing to TCP-probe."""
    monkeypatch.setattr(main, "describe_settings", lambda: _describe([]))
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "gpt-image-1")
    monkeypatch.setattr(
        local_health, "is_reachable", lambda _url: pytest.fail("must not probe")
    )
    main._warn_if_local_model_unreachable()


def test_shared_text_and_image_server_is_probed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same LOCAL_ENDPOINTS entry can name a text server and an image
    server; the probe dedupes by base URL, so it costs one socket, not two."""
    monkeypatch.setattr(
        main, "describe_settings", lambda: _describe(["local:sd/llama-3.1-8b"])
    )
    monkeypatch.setenv("IMAGE_GENERATION", "true")
    monkeypatch.setenv("IMAGE_GENERATION_MODEL", "local:sd/default")
    monkeypatch.setenv("LOCAL_ENDPOINTS", '{"sd": "http://localhost:7860/v1"}')
    probed: list[str] = []

    def unreachable(url: str) -> bool:
        probed.append(url)
        return False

    monkeypatch.setattr(local_health, "is_reachable", unreachable)
    main._warn_if_local_model_unreachable()
    assert probed == ["http://localhost:7860/v1"]
