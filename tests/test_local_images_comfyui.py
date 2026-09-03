"""ComfyUI local image backend (app/local_images_comfyui.py): the graph it
builds, how a custom workflow is filled in, the submit / poll / fetch
protocol against a fake server, and the never-raises contract.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app import local_images, local_images_comfyui as comfy
from app.local_images_comfyui import (
    base_url,
    build_graph,
    default_graph,
    fill_graph,
    generate_images_comfyui,
)

MODEL = "local:comfy/sd_xl_base_1.0.safetensors"


def _response(payload=None, status: int = 200, content: bytes = b"", ctype: str = ""):
    def raise_for_status() -> None:
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")

    return SimpleNamespace(
        json=lambda: payload,
        raise_for_status=raise_for_status,
        content=content,
        headers={"content-type": ctype} if ctype else {},
    )


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LOCAL_ENDPOINTS", json.dumps({"comfy": "http://localhost:8188/v1"})
    )
    monkeypatch.setattr(comfy.time, "sleep", lambda _s: None)


# --- URL shape ------------------------------------------------------------------


def test_base_url_strips_the_documented_v1_suffix() -> None:
    assert base_url("http://localhost:8188/v1") == "http://localhost:8188"
    assert base_url("http://localhost:8188/") == "http://localhost:8188"
    assert base_url("http://box:8188/V1/") == "http://box:8188"


# --- the built-in graph ---------------------------------------------------------


def test_default_graph_is_comfyuis_stock_text_to_image_pipeline() -> None:
    graph = default_graph("sd_xl_base_1.0.safetensors", "a cat", "768x512", seed=42)
    assert graph["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert graph["6"]["inputs"]["text"] == "a cat"
    assert graph["5"]["inputs"] == {"width": 768, "height": 512, "batch_size": 1}
    assert graph["3"]["inputs"]["seed"] == 42
    assert graph["3"]["inputs"]["positive"] == ["6", 0]
    assert graph["9"]["class_type"] == "SaveImage"
    # Every link points at a node that exists.
    for node in graph.values():
        for value in node["inputs"].values():
            if isinstance(value, list):
                assert str(value[0]) in graph


def test_build_graph_refuses_the_a1111_default_checkpoint(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ComfyUI has no "whatever is loaded"; the graph needs a filename."""
    monkeypatch.delenv("COMFYUI_WORKFLOW", raising=False)
    assert build_graph("default", "a cat", "auto") is None
    assert "images.comfyui_unconfigured" in caplog.text
    assert "COMFYUI_WORKFLOW" in caplog.text


def test_build_graph_seeds_differ_between_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ComfyUI caches by graph: an identical graph returns the previous
    image without rendering."""
    monkeypatch.delenv("COMFYUI_WORKFLOW", raising=False)
    seeds = {
        build_graph("x.safetensors", "a cat", "auto")["3"]["inputs"]["seed"]
        for _ in range(5)
    }  # type: ignore[index]
    assert len(seeds) > 1


# --- a custom exported workflow -------------------------------------------------

_EXPORTED = {
    "10": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 1,
            "positive": ["12", 0],
            "negative": ["13", 0],
            "model": ["11", 0],
            "latent_image": ["14", 0],
        },
        "_meta": {"title": "KSampler"},
    },
    "11": {
        "class_type": "UNETLoader",
        "inputs": {"unet_name": "flux1-dev.safetensors"},
    },
    "12": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "old prompt", "clip": ["15", 0]},
    },
    "13": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry", "clip": ["15", 0]},
    },
    "14": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 512, "height": 512, "batch_size": 1},
    },
    "15": {
        "class_type": "DualCLIPLoader",
        "inputs": {"clip_name1": "a", "clip_name2": "b"},
    },
}


def test_fill_graph_writes_the_prompt_into_the_ksamplers_positive_node() -> None:
    filled = fill_graph(_EXPORTED, "ignored", "a red fox", "auto", seed=7)
    assert filled["12"]["inputs"]["text"] == "a red fox"
    assert filled["13"]["inputs"]["text"] == "blurry"  # the negative is untouched
    assert filled["10"]["inputs"]["seed"] == 7
    assert _EXPORTED["12"]["inputs"]["text"] == "old prompt"  # never mutated


def test_fill_graph_substitutes_placeholders_anywhere() -> None:
    graph = {
        "1": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "masterpiece, {prompt}"},
        },
        "2": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "{checkpoint}"},
        },
        "3": {
            "class_type": "PrimitiveNode",
            "inputs": {"value": "{width}x{height} seed {seed}"},
        },
    }
    filled = fill_graph(graph, "sd.safetensors", "a cat", "640x480", seed=9)
    assert filled["1"]["inputs"]["text"] == "masterpiece, a cat"
    assert filled["2"]["inputs"]["ckpt_name"] == "sd.safetensors"
    assert filled["3"]["inputs"]["value"] == "640x480 seed 9"


def test_fill_graph_with_a_prompt_placeholder_leaves_the_positive_node_alone() -> None:
    graph = json.loads(json.dumps(_EXPORTED))
    graph["12"]["inputs"]["text"] = "style tokens, {prompt}"
    filled = fill_graph(graph, "x", "a cat", "auto", seed=1)
    assert filled["12"]["inputs"]["text"] == "style tokens, a cat"


def test_build_graph_reads_the_workflow_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    path = tmp_path / "flow.json"
    path.write_text(json.dumps(_EXPORTED))
    monkeypatch.setenv("COMFYUI_WORKFLOW", str(path))
    graph = build_graph("default", "a cat", "auto")
    assert graph is not None
    assert graph["12"]["inputs"]["text"] == "a cat"


def test_build_graph_rejects_a_ui_format_export(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Plain "Save" exports the UI layout ({nodes: [...], links: [...]}),
    which the server does not accept; say so rather than submitting it."""
    path = tmp_path / "ui.json"
    path.write_text(json.dumps({"nodes": [], "links": [], "version": 0.4}))
    monkeypatch.setenv("COMFYUI_WORKFLOW", str(path))
    assert build_graph("x", "a cat", "auto") is None
    assert "Save (API Format)" in caplog.text


def test_build_graph_missing_workflow_file_is_configuration_not_an_outage(
    monkeypatch: pytest.MonkeyPatch, tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("COMFYUI_WORKFLOW", str(tmp_path / "nope.json"))
    assert build_graph("x", "a cat", "auto") is None
    assert "images.comfyui_unconfigured" in caplog.text


# --- the protocol against a fake server -----------------------------------------


class _FakeServer:
    """Answers /prompt, /history/<id> (queued for `pending` polls, then done)
    and /view, recording everything it was sent."""

    def __init__(self, pending: int = 1, images=None, node_errors=None, status=None):
        self.pending = pending
        self.images = (
            images
            if images is not None
            else [{"filename": "out_00001_.png", "subfolder": "", "type": "output"}]
        )
        self.node_errors = node_errors
        self.status = status
        self.posts: list[dict] = []
        self.gets: list[str] = []

    def post(self, url, *, json, timeout):
        self.posts.append({"url": url, "body": json, "timeout": timeout})
        if self.node_errors:
            return _response(
                {"error": "invalid prompt", "node_errors": self.node_errors}
            )
        return _response({"prompt_id": "abc", "number": 1, "node_errors": {}})

    def get(self, url, *, timeout, params=None):
        self.gets.append(url)
        if "/history/" in url:
            if self.pending > 0:
                self.pending -= 1
                return _response({})  # not in history until finished
            entry: dict = {"outputs": {"9": {"images": self.images}}}
            if self.status:
                entry["status"] = self.status
                entry["outputs"] = {}
            return _response({"abc": entry})
        if url.endswith("/view"):
            assert params["filename"] == self.images[0]["filename"]
            return _response(content=b"PNGBYTES", ctype="image/png; charset=binary")
        raise AssertionError(url)


def _install(monkeypatch: pytest.MonkeyPatch, server: _FakeServer) -> None:
    monkeypatch.setattr(comfy.httpx, "post", server.post)
    monkeypatch.setattr(comfy.httpx, "get", server.get)


def test_generate_submits_polls_and_fetches(
    monkeypatch: pytest.MonkeyPatch, endpoint
) -> None:
    server = _FakeServer(pending=2)
    _install(monkeypatch, server)

    images = generate_images_comfyui(MODEL, "a cat", "1024x1024")

    expected = "data:image/png;base64," + base64.b64encode(b"PNGBYTES").decode()
    assert images == [expected]
    assert server.posts[0]["url"] == "http://localhost:8188/prompt"
    body = server.posts[0]["body"]
    assert body["client_id"]
    assert body["prompt"]["4"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert body["prompt"]["6"]["inputs"]["text"] == "a cat"
    # Two "not yet" polls, one that found it, then the fetch.
    assert server.gets.count("http://localhost:8188/history/abc") == 3
    assert server.gets[-1] == "http://localhost:8188/view"


def test_generate_a_refused_graph_returns_empty_and_logs_the_node_errors(
    monkeypatch: pytest.MonkeyPatch, endpoint, caplog: pytest.LogCaptureFixture
) -> None:
    server = _FakeServer(
        node_errors={"4": {"errors": [{"message": "Value not in list: ckpt_name"}]}}
    )
    _install(monkeypatch, server)
    assert generate_images_comfyui(MODEL, "a cat", "auto") == []
    assert "images.comfyui_rejected" in caplog.text
    assert "ckpt_name" in caplog.text
    assert not server.gets  # never polled


def test_generate_an_errored_job_returns_empty(
    monkeypatch: pytest.MonkeyPatch, endpoint, caplog: pytest.LogCaptureFixture
) -> None:
    server = _FakeServer(
        pending=0,
        status={
            "status_str": "error",
            "messages": [
                ["execution_error", {"exception_message": "CUDA out of memory"}]
            ],
        },
    )
    _install(monkeypatch, server)
    assert generate_images_comfyui(MODEL, "a cat", "auto") == []
    assert "images.comfyui_generate_failed" in caplog.text
    assert "CUDA out of memory" in caplog.text


def test_generate_gives_up_at_the_timeout(
    monkeypatch: pytest.MonkeyPatch, endpoint, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LOCAL_IMAGE_TIMEOUT", "5")
    clock = iter([0.0, 0.0, 2.0, 4.0, 6.0, 8.0, 10.0])
    monkeypatch.setattr(comfy.time, "monotonic", lambda: next(clock))
    server = _FakeServer(pending=100)
    _install(monkeypatch, server)

    assert generate_images_comfyui(MODEL, "a cat", "auto") == []
    assert "images.comfyui_timed_out" in caplog.text
    assert len(server.gets) < 10


def test_generate_unconfigured_name_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("LOCAL_ENDPOINTS", json.dumps({"other": "http://x:1"}))
    monkeypatch.setattr(
        comfy.httpx, "post", lambda *a, **k: pytest.fail("must not be called")
    )
    assert generate_images_comfyui(MODEL, "a cat", "auto") == []
    assert "images.comfyui_unconfigured" in caplog.text


def test_generate_unreachable_server_returns_empty_and_logs(
    monkeypatch: pytest.MonkeyPatch, endpoint, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(comfy.httpx, "post", refuse)
    assert generate_images_comfyui(MODEL, "a cat", "auto") == []
    assert "images.comfyui_generate_failed" in caplog.text


def test_generate_fetches_every_output_image(
    monkeypatch: pytest.MonkeyPatch, endpoint
) -> None:
    server = _FakeServer(
        pending=0,
        images=[
            {"filename": "a.png", "subfolder": "", "type": "output"},
            {"filename": "a.png", "subfolder": "", "type": "output"},
        ],
    )
    _install(monkeypatch, server)
    assert len(generate_images_comfyui(MODEL, "a cat", "auto")) == 2


# --- the fork in the A1111 module ---------------------------------------------------


def test_local_image_api_defaults_to_a1111_and_warns_on_a_typo(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("LOCAL_IMAGE_API", raising=False)
    assert local_images.local_image_api() == "a1111"
    monkeypatch.setenv("LOCAL_IMAGE_API", " ComfyUI ")
    assert local_images.local_image_api() == "comfyui"
    monkeypatch.setenv("LOCAL_IMAGE_API", "comfy")
    assert local_images.local_image_api() == "a1111"
    assert "images.local_api_unknown" in caplog.text


def test_generate_images_local_forks_to_comfyui(
    monkeypatch: pytest.MonkeyPatch, endpoint
) -> None:
    monkeypatch.setenv("LOCAL_IMAGE_API", "comfyui")
    monkeypatch.setattr(
        local_images.httpx,
        "post",
        lambda *a, **k: pytest.fail("A1111 path must not run"),
    )
    server = _FakeServer(pending=0)
    _install(monkeypatch, server)
    assert len(local_images.generate_images_local(MODEL, "a cat", "auto")) == 1
    assert server.posts[0]["url"].endswith("/prompt")


def test_generate_images_local_default_stays_on_a1111(
    monkeypatch: pytest.MonkeyPatch, endpoint
) -> None:
    monkeypatch.delenv("LOCAL_IMAGE_API", raising=False)
    monkeypatch.setattr(
        comfy.httpx, "post", lambda *a, **k: pytest.fail("ComfyUI path must not run")
    )
    monkeypatch.setattr(
        local_images.httpx, "post", lambda *a, **k: _response({"images": ["aaa"]})
    )
    assert local_images.generate_images_local(MODEL, "a cat", "auto") == [
        "data:image/png;base64,aaa"
    ]
