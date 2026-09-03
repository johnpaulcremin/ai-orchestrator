"""Generates images on a locally running ComfyUI server at $0 through its
native workflow-graph API — submit a graph, poll the history, fetch the
outputs — never raising: any failure returns an empty list with a log line
naming its cause.

The second local image backend beside app/local_images.py (AUTOMATIC1111's
synchronous `txt2img`), selected by `LOCAL_IMAGE_API=comfyui`. Same
`local:<name>/<checkpoint>` model id, same LOCAL_ENDPOINTS name, same $0
pricing and budget immunity; a different wire protocol, which is why it is a
module of its own rather than a branch in the other one:

  1. `POST /prompt` with `{"prompt": <graph>, "client_id": ...}` queues a
     job and answers `{"prompt_id": ...}` at once — or `node_errors` when
     the graph does not validate against the server's installed nodes.
  2. `GET /history/<prompt_id>` is polled until the job's entry appears with
     `outputs`; ComfyUI has no blocking call, and its websocket progress
     feed is more than one image needs.
  3. Each `outputs[<node>]["images"]` entry names a file, fetched with
     `GET /view?filename=&subfolder=&type=` and returned as a data URL.

A graph per request is the whole point of ComfyUI, so the graph comes from
one of two places. With no `COMFYUI_WORKFLOW` set, a built-in text-to-image
graph is used — checkpoint loader, positive and empty negative prompt,
empty latent at the requested size, KSampler, VAE decode, save — the graph
ComfyUI's own default workspace produces, in the "API format" its
`POST /prompt` accepts. That graph needs a real checkpoint filename, so
`<checkpoint>` is mandatory here (`default` is an A1111 convention; ComfyUI
has no "whatever is loaded"). With `COMFYUI_WORKFLOW` pointing at a
workflow exported from ComfyUI with "Save (API Format)", that graph is used
instead, which is how a Flux, SDXL-plus-refiner or ControlNet pipeline gets
in without this module knowing anything about it. The per-request values
are folded into a custom graph two ways: any string input containing
`{prompt}`, `{width}`, `{height}`, `{checkpoint}` or `{seed}` is
substituted, and when no `{prompt}` placeholder exists the positive prompt
is written into the `CLIPTextEncode` node the first `KSampler`'s `positive`
input points at — the shape every exported text-to-image graph has.

Every `KSampler` seed that is a literal number is replaced per request. Not
for variety: ComfyUI caches by graph, and an identical graph returns the
previous image without rendering, so "draw me a cat" twice would show the
same cat and look like a bug.

Same never-raises contract and the same distinct log lines as the A1111
module: `images.comfyui_unconfigured` for a name missing from
LOCAL_ENDPOINTS or a graph that cannot be built, `images.comfyui_rejected`
for a graph the server refused, `images.comfyui_generate_failed` for a
server that did not answer, and `images.comfyui_timed_out` for a job that
was accepted and never finished within LOCAL_IMAGE_TIMEOUT.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from . import local_endpoints
from .local_images import parse_size, timeout_seconds
from .telemetry import logger

# How often the history endpoint is asked whether the job finished. A
# render takes seconds to minutes, so once a second is plenty and keeps a
# slow CPU box from being hammered while it works.
_POLL_INTERVAL_SECONDS = 1.0
# Bounded per call so a stalled server cannot hold a request open past the
# operator's timeout; the total wait is LOCAL_IMAGE_TIMEOUT.
_HTTP_TIMEOUT_SECONDS = 30.0

_PLACEHOLDERS = ("{prompt}", "{width}", "{height}", "{checkpoint}", "{seed}")
_NO_CHECKPOINT = "default"


def base_url(configured: str) -> str:
    """The server root for a LOCAL_ENDPOINTS value, tolerating the `/v1`
    suffix the text scheme documents (ComfyUI's API lives at the root)."""
    base = configured.strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def workflow_path() -> str:
    return (os.getenv("COMFYUI_WORKFLOW") or "").strip()


def default_graph(checkpoint: str, prompt: str, size: str, seed: int) -> dict[str, Any]:
    """ComfyUI's stock text-to-image graph in API format. Node ids are the
    ones the default workspace exports, so an operator comparing this with
    their own export sees the same numbers."""
    width, height = parse_size(size)
    return {
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": checkpoint},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "", "clip": ["4", 1]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "ai-orchestrator", "images": ["8", 0]},
        },
    }


def _substitute(value: Any, values: dict[str, str]) -> Any:
    if isinstance(value, str) and any(p in value for p in _PLACEHOLDERS):
        for placeholder, replacement in values.items():
            value = value.replace(placeholder, replacement)
    return value


def _positive_prompt_node(graph: dict[str, Any]) -> str | None:
    """The id of the CLIPTextEncode node the first KSampler's `positive`
    input points at, or None when the graph has no such shape."""
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "KSampler":
            continue
        positive = (node.get("inputs") or {}).get("positive")
        if isinstance(positive, list) and positive:
            target = graph.get(str(positive[0]))
            if (
                isinstance(target, dict)
                and target.get("class_type") == "CLIPTextEncode"
            ):
                return str(positive[0])
    return None


def fill_graph(
    graph: dict[str, Any], checkpoint: str, prompt: str, size: str, seed: int
) -> dict[str, Any]:
    """A custom (exported) graph with this request's values folded in. The
    original is never mutated: the operator's workflow is read once and
    reused for every request."""
    width, height = parse_size(size)
    filled = copy.deepcopy(graph)
    values = {
        "{prompt}": prompt,
        "{width}": str(width),
        "{height}": str(height),
        "{checkpoint}": checkpoint,
        "{seed}": str(seed),
    }
    has_prompt_placeholder = False
    for node in filled.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, value in list(inputs.items()):
            if isinstance(value, str) and "{prompt}" in value:
                has_prompt_placeholder = True
            inputs[key] = _substitute(value, values)
        if node.get("class_type") == "KSampler" and isinstance(inputs.get("seed"), int):
            inputs["seed"] = seed
    if not has_prompt_placeholder:
        target = _positive_prompt_node(filled)
        if target is not None:
            filled[target]["inputs"]["text"] = prompt
    return filled


def build_graph(checkpoint: str, prompt: str, size: str) -> dict[str, Any] | None:
    """The graph to submit for this request, or None (logged) when none can
    be built: no workflow file and no checkpoint, or a workflow file that
    cannot be read or is not an API-format graph."""
    seed = random.randint(0, 2**53 - 1)  # noqa: S311 — not a secret, a sampler seed
    path = workflow_path()
    if not path:
        if not checkpoint or checkpoint.strip().lower() == _NO_CHECKPOINT:
            logger.warning(
                "images.comfyui_unconfigured — ComfyUI's built-in graph needs a "
                "checkpoint filename: set IMAGE_GENERATION_MODEL to "
                "local:<name>/<file>.safetensors, or point COMFYUI_WORKFLOW at an "
                "exported API-format workflow"
            )
            return None
        return default_graph(checkpoint, prompt, size, seed)
    try:
        graph = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        logger.exception(
            "images.comfyui_unconfigured — COMFYUI_WORKFLOW=%s unreadable", path
        )
        return None
    if not isinstance(graph, dict) or not any(
        isinstance(node, dict) and "class_type" in node for node in graph.values()
    ):
        logger.warning(
            "images.comfyui_unconfigured — COMFYUI_WORKFLOW=%s is not an API-format "
            'graph (export it from ComfyUI with "Save (API Format)", not "Save")',
            path,
        )
        return None
    return fill_graph(graph, checkpoint, prompt, size, seed)


def _submit(root: str, graph: dict[str, Any]) -> str | None:
    response = httpx.post(
        f"{root}/prompt",
        json={"prompt": graph, "client_id": uuid.uuid4().hex},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return None
    if data.get("node_errors") or data.get("error"):
        logger.warning(
            "images.comfyui_rejected — the server refused the graph: %s",
            json.dumps(data.get("node_errors") or data.get("error"))[:800],
        )
        return None
    prompt_id = data.get("prompt_id")
    return str(prompt_id) if prompt_id else None


def _wait_for_outputs(
    root: str, prompt_id: str, deadline: float
) -> dict[str, Any] | None:
    """The job's `outputs` map once it exists, None on timeout or an error
    status. Polls the history endpoint; the job is absent from it until it
    finishes."""
    while True:
        response = httpx.get(
            f"{root}/history/{prompt_id}", timeout=_HTTP_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        data = response.json()
        entry = data.get(prompt_id) if isinstance(data, dict) else None
        if isinstance(entry, dict):
            status = entry.get("status") or {}
            if isinstance(status, dict) and status.get("status_str") == "error":
                logger.warning(
                    "images.comfyui_generate_failed prompt_id=%s — the job errored: %s",
                    prompt_id,
                    json.dumps(status.get("messages", []))[:800],
                )
                return None
            outputs = entry.get("outputs")
            if isinstance(outputs, dict) and outputs:
                return outputs
        if time.monotonic() >= deadline:
            logger.warning(
                "images.comfyui_timed_out prompt_id=%s — not finished within "
                "LOCAL_IMAGE_TIMEOUT (%ss); the job may still be rendering",
                prompt_id,
                timeout_seconds(),
            )
            return None
        time.sleep(_POLL_INTERVAL_SECONDS)


def _fetch_images(root: str, outputs: dict[str, Any]) -> list[str]:
    images: list[str] = []
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        for item in node_output.get("images") or []:
            if not isinstance(item, dict) or not item.get("filename"):
                continue
            response = httpx.get(
                f"{root}/view",
                params={
                    "filename": item["filename"],
                    "subfolder": item.get("subfolder", ""),
                    "type": item.get("type", "output"),
                },
                timeout=_HTTP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "image/png").split(
                ";"
            )[0]
            encoded = base64.b64encode(response.content).decode("ascii")
            images.append(f"data:{content_type};base64,{encoded}")
    return images


def generate_images_comfyui(model: str, prompt: str, size: str) -> list[str]:
    """Render `prompt` on the ComfyUI server `model` names, as ready-to-render
    data URLs — or `[]`, logged, on any failure.

    `model` is `local:<name>/<checkpoint>`, the same id the A1111 backend
    takes; which of the two speaks to the server is LOCAL_IMAGE_API's call.
    """
    parsed = local_endpoints.parse(model)
    configured = local_endpoints.base_url_for(model)
    if parsed is None or not configured:
        logger.warning(
            "images.comfyui_unconfigured model=%s — its LOCAL_ENDPOINTS name is not "
            "configured, so there is no server to send the graph to",
            model,
        )
        return []
    _name, checkpoint = parsed
    graph = build_graph(checkpoint, prompt, size)
    if graph is None:
        return []
    root = base_url(configured)
    deadline = time.monotonic() + timeout_seconds()
    try:
        prompt_id = _submit(root, graph)
        if prompt_id is None:
            return []
        outputs = _wait_for_outputs(root, prompt_id, deadline)
        if outputs is None:
            return []
        return _fetch_images(root, outputs)
    except Exception:
        logger.exception("images.comfyui_generate_failed model=%s url=%s", model, root)
        return []
