"""Generates images on a locally running Stable Diffusion server at $0, never
raising: any failure returns an empty list with a log line naming its cause.

The image-side twin of app/local_endpoints.py's text story. Point
IMAGE_GENERATION_MODEL at `local:<name>/<checkpoint>` and the standalone
image call goes to the LOCAL_ENDPOINTS server named `<name>` instead of a
paid provider — no API key, `cost_usd: 0`, and (like every other `local:`
model) immune to DAILY_BUDGET_USD. Only worth configuring on a machine with
a GPU; a CPU render of a 1024x1024 image can take minutes, which is why the
timeout here defaults so much higher than any other HTTP call in this app.

Speaks the AUTOMATIC1111 web-API surface — `POST /sdapi/v1/txt2img`, which
returns base64 PNGs synchronously — because it is the de-facto local
image API: Forge, SD.Next, and ComfyUI-with-the-A1111-bridge all serve it.
ComfyUI's NATIVE API is a different shape entirely (a workflow graph per
request, then a poll, then a fetch) and is deliberately out of scope; it
would be a second backend, not a variant of this one.

Two facts about that surface shape this module:

  * The API is NOT under `/v1`. LOCAL_ENDPOINTS values are OpenAI-compatible
    bases and conventionally end in `/v1` (`http://host:7860/v1`); A1111's
    endpoint is `http://host:7860/sdapi/v1/txt2img`. So a trailing `/v1` on
    the configured base is stripped before the path is appended — the same
    entry can name a text server and an image server, and an operator who
    copies the documented `/v1` form is not silently sent to a 404.

  * `<checkpoint>` selects the model the server loads, via A1111's
    `override_settings.sd_model_checkpoint` — the one knob that makes the
    model id in this app mean something. The literal `default` opts out
    and uses whatever the server already has loaded, for the common case of
    a single-model server where naming the file would only invite a typo.

Same never-raises contract as providers.generate_images_litellm, and it
matters more here: a dead local server is the EXPECTED failure mode (the
operator has not started it yet), and it must surface as the ordinary
"image failed" note plus a log line, never as a 500. The two causes are
logged distinctly — `images.local_unconfigured` for a name missing from
LOCAL_ENDPOINTS, `images.local_generate_failed` for a server that was
named but did not answer — because the fixes are different and a single
"failed" would send the reader to the wrong one.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx

from . import local_endpoints
from .telemetry import logger

# A1111 has no "auto" size; every request needs explicit pixels. 1024 square
# is SDXL's native resolution and a safe default for SD 1.5 servers too.
_DEFAULT_SIDE = 1024
_SIZE_RE = re.compile(r"^\s*(\d{2,5})\s*[xX×]\s*(\d{2,5})\s*$")

# Deliberately generous. Every other outbound call here is a hosted API that
# answers in seconds; a local diffusion render on modest hardware does not.
# Bounding it at all is what keeps a request from hanging forever on a server
# that accepted the job and then stalled.
_DEFAULT_TIMEOUT_SECONDS = 300.0

# The A1111 checkpoint override is skipped for this model name.
_NO_OVERRIDE = "default"


def txt2img_url(base_url: str) -> str:
    """The `/sdapi/v1/txt2img` URL for a LOCAL_ENDPOINTS base, tolerating the
    `/v1` suffix the text scheme documents (it is stripped, not doubled)."""
    base = base_url.strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/sdapi/v1/txt2img"


def parse_size(size: str) -> tuple[int, int]:
    """(width, height) for an IMAGE_GENERATION_SIZE value. `auto`, blank, or
    anything unparseable falls back to the square default rather than
    erroring — a bad size setting should not be what makes images fail."""
    match = _SIZE_RE.match(size or "")
    if not match:
        return _DEFAULT_SIDE, _DEFAULT_SIDE
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return _DEFAULT_SIDE, _DEFAULT_SIDE
    return width, height


def timeout_seconds() -> float:
    """LOCAL_IMAGE_TIMEOUT in seconds; the generous default when unset,
    unparseable, or non-positive (a zero timeout means "no timeout" to some
    HTTP clients — the opposite of what an operator typing 0 intends)."""
    raw = (os.getenv("LOCAL_IMAGE_TIMEOUT") or "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS


def request_body(checkpoint: str, prompt: str, size: str) -> dict[str, Any]:
    """The JSON A1111 is sent. Exposed for tests, so the body can be asserted
    without a fake server."""
    width, height = parse_size(size)
    body: dict[str, Any] = {
        "prompt": prompt,
        "width": width,
        "height": height,
        "batch_size": 1,
        "n_iter": 1,
    }
    if checkpoint and checkpoint.strip().lower() != _NO_OVERRIDE:
        body["override_settings"] = {"sd_model_checkpoint": checkpoint}
        # Restore the server's previous checkpoint afterwards, so this call
        # does not silently repoint a shared server for the next user.
        body["override_settings_restore_afterwards"] = True
    return body


def generate_images_local(model: str, prompt: str, size: str) -> list[str]:
    """Render `prompt` on the local server `model` names, as ready-to-render
    `data:image/png;base64,...` URLs — or `[]`, logged, on any failure.

    `model` is `local:<name>/<checkpoint>`. A name not in LOCAL_ENDPOINTS has
    nowhere to go and is logged as configuration, not as an outage.
    """
    parsed = local_endpoints.parse(model)
    base_url = local_endpoints.base_url_for(model)
    if parsed is None or not base_url:
        logger.warning(
            "images.local_unconfigured model=%s — its LOCAL_ENDPOINTS name is not "
            "configured, so there is no server to send the request to",
            model,
        )
        return []
    _name, checkpoint = parsed
    url = txt2img_url(base_url)
    try:
        response = httpx.post(
            url,
            json=request_body(checkpoint, prompt, size),
            timeout=timeout_seconds(),
        )
        response.raise_for_status()
        data = response.json()
    except Exception:
        logger.exception("images.local_generate_failed model=%s url=%s", model, url)
        return []

    images: list[str] = []
    for item in (data.get("images") if isinstance(data, dict) else None) or []:
        if isinstance(item, str) and item:
            # A1111 returns bare base64; some forks prefix a data: header
            # already. Accept both rather than double-wrapping the second.
            images.append(
                item if item.startswith("data:") else f"data:image/png;base64,{item}"
            )
    return images
