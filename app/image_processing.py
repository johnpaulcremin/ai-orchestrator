"""Automatic image-token cost reduction for vision input, skipped when the
question implies fine detail matters, so a diagram or small text is never
silently degraded.

The exemption belongs in the first sentence — it is the difference between
a cost optimisation and a quality bug, and a summary that omitted it
invited exactly the critique that this could harm legibility (see
wants_fine_detail).

Two independent, automatic (no user toggle needed) transformations applied to
attached images before they're sent to a model — never touching what's
persisted with the message, only what the model actually receives:

1. Downscaling: a large image is resized down to a bounded resolution before
   sending, since vision APIs tokenize images roughly proportional to pixel
   count — a 10x-smaller image can mean an order of magnitude fewer tokens
   for content where fine detail doesn't matter (a screenshot, a rough
   diagram). Skipped for images already at/under the cap.
2. OCR replacement: if an image is mostly text (a screenshot, a document
   page, code) OCR extracts it locally (no API call) and — only when
   confident and dense enough — that plain text is sent instead of the image
   entirely, which is normally far cheaper than image tokens for the same
   content, and lets the model reason over clean text instead of "reading" a
   picture.

Both fail SAFE: any decode/library/binary error just sends the original
image unchanged, and the question text is checked for a "fine detail wanted"
signal (e.g. "read the small text", "zoom in") that skips both transforms
entirely — full quality, no OCR substitution — for images/questions where a
degraded or textual view of the image genuinely isn't good enough.
"""

from __future__ import annotations

import base64
import binascii
import io
import os
import re

from .settings import bool_setting
from .telemetry import logger

_DATA_IMAGE_URL_RE = re.compile(
    r"^data:image/(png|jpe?g|gif|webp);base64,([A-Za-z0-9+/]+=*)$"
)

# Below this, resizing wouldn't meaningfully reduce tokens — skip the work.
_DEFAULT_DOWNSCALE_MAX_DIMENSION = 1024

# pytesseract's image_to_data confidence is 0-100 per word (-1 for non-text
# regions); the mean of the real (>=0) values must clear this bar, and the
# extracted text must be long enough, before OCR replaces the image outright
# — a few confidently-read words isn't enough evidence the image is "mostly
# text" rather than a photo/diagram with an incidental label.
_OCR_MIN_MEAN_CONFIDENCE = 60.0
_OCR_MIN_CHARS = 40

# A question implying the user needs to actually SEE fine detail — skip both
# downscaling and OCR replacement entirely rather than risk answering from a
# degraded or text-only view. Deliberately narrow/high-precision, same
# bias as orchestrator_tools._looks_like_image_request: false negatives (missing a
# detail-wanted question) just mean an unnecessary downscale, not a wrong
# answer; false positives (skipping the optimization) cost a few tokens, no
# correctness risk either way.
_FINE_DETAIL_PHRASES = (
    "read the",
    "small text",
    "small print",
    "fine print",
    "zoom in",
    "exact text",
    "exact wording",
    "precisely",
    "pixel",
    "tiny",
    "illegible",
    "hard to read",
)


def wants_fine_detail(question: str) -> bool:
    lowered = (question or "").lower()
    return any(phrase in lowered for phrase in _FINE_DETAIL_PHRASES)


def _downscale_enabled() -> bool:
    return bool_setting("IMAGE_DOWNSCALE", True)


def _ocr_replacement_enabled() -> bool:
    return bool_setting("OCR_REPLACEMENT", True)


def _downscale_max_dimension() -> int:
    raw = (os.getenv("IMAGE_DOWNSCALE_MAX_DIMENSION") or "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_DOWNSCALE_MAX_DIMENSION
    except ValueError:
        return _DEFAULT_DOWNSCALE_MAX_DIMENSION
    return value if value > 0 else _DEFAULT_DOWNSCALE_MAX_DIMENSION


def _decode_data_url(url: str) -> tuple[str, bytes] | None:
    match = _DATA_IMAGE_URL_RE.match(url.strip())
    if not match:
        return None
    mime, b64 = match.group(1), match.group(2)
    try:
        return mime, base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None


def downscale_image_data_url(url: str, max_dimension: int | None = None) -> str:
    """Resize the image to fit within max_dimension x max_dimension, re-encoded
    as a data URL in its original format. Returns `url` unchanged on any
    decode/library failure, or if it's already small enough — never raises.
    """
    decoded = _decode_data_url(url)
    if decoded is None:
        return url
    _mime, raw = decoded
    cap = max_dimension if max_dimension is not None else _downscale_max_dimension()
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as opened:
            width, height = opened.size
            if width <= cap and height <= cap:
                return url
            image_format = (opened.format or "PNG").upper()
            image = opened.copy()

        image.thumbnail((cap, cap), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        if image_format in ("JPEG", "JPG") and image.mode in ("RGBA", "P"):
            image = image.convert("RGB")
        save_kwargs = {"quality": 85} if image_format in ("JPEG", "JPG") else {}
        image.save(buffer, format=image_format, **save_kwargs)
        new_b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
        mime_subtype = (
            "jpeg" if image_format in ("JPEG", "JPG") else image_format.lower()
        )
        return f"data:image/{mime_subtype};base64,{new_b64}"
    except Exception:
        logger.warning("image_processing.downscale_failed", exc_info=True)
        return url


_tesseract_available_cache: bool | None = None


def _tesseract_available() -> bool:
    """Whether the Tesseract OCR binary is actually reachable. Cached after
    the first check (a missing binary won't reappear mid-process) so a
    disabled/uninstalled setup doesn't pay a subprocess-probe cost on every
    request — this is checked at most once per process, never per image."""
    global _tesseract_available_cache
    if _tesseract_available_cache is not None:
        return _tesseract_available_cache

    cmd = (os.getenv("TESSERACT_CMD") or "").strip()
    try:
        import pytesseract

        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        pytesseract.get_tesseract_version()
        _tesseract_available_cache = True
    except Exception:
        _tesseract_available_cache = False
    return _tesseract_available_cache


def reset_tesseract_cache() -> None:
    """Test-only: clear the cached availability check."""
    global _tesseract_available_cache
    _tesseract_available_cache = None


def ocr_extract(url: str) -> tuple[str, float] | None:
    """(text, mean_confidence) extracted from the image, or None if OCR
    isn't available/configured or extraction fails for any reason."""
    if not _tesseract_available():
        return None
    decoded = _decode_data_url(url)
    if decoded is None:
        return None
    _mime, raw = decoded
    try:
        import pytesseract
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
            confidences = [float(c) for c in data.get("conf", []) if float(c) >= 0]
            mean_confidence = (
                sum(confidences) / len(confidences) if confidences else 0.0
            )
            text = " ".join(word for word in data.get("text", []) if word.strip())
            return text, mean_confidence
    except Exception:
        logger.warning("image_processing.ocr_failed", exc_info=True)
        return None


def process_images(
    images: list[str] | None, question: str
) -> tuple[list[str] | None, str | None, str | None]:
    """The images to actually send, an OCR-extracted-text appendix to fold
    into the question (or None), and a short note describing what happened
    (or None if neither transform applied) — for the answer's `notes` field,
    so the substitution is visible on inspection rather than silent.

    Returns `(images, None, None)` unchanged whenever there's nothing to
    process, both transforms are disabled, or the question signals the user
    needs the image at full, unmodified quality (see wants_fine_detail).
    """
    if not images:
        return images, None, None
    if wants_fine_detail(question):
        return images, None, None

    downscale_on = _downscale_enabled()
    ocr_on = _ocr_replacement_enabled()

    kept_images: list[str] = []
    ocr_texts: list[str] = []
    downscaled_count = 0

    for url in images:
        if ocr_on:
            extracted = ocr_extract(url)
            if extracted is not None:
                text, confidence = extracted
                if (
                    confidence >= _OCR_MIN_MEAN_CONFIDENCE
                    and len(text.strip()) >= _OCR_MIN_CHARS
                ):
                    ocr_texts.append(text.strip())
                    continue
        if downscale_on:
            resized = downscale_image_data_url(url)
            if resized != url:
                downscaled_count += 1
            kept_images.append(resized)
        else:
            kept_images.append(url)

    appendix = None
    if ocr_texts:
        blocks = "\n---\n".join(ocr_texts)
        appendix = f"\n\nExtracted text from attached image(s) (OCR):\n{blocks}"

    notes_parts = []
    if ocr_texts:
        notes_parts.append(f"ocr_replaced={len(ocr_texts)}")
    if downscaled_count:
        notes_parts.append(f"downscaled={downscaled_count}")
    note = f"image_preprocessing: {', '.join(notes_parts)}" if notes_parts else None

    return (kept_images or None), appendix, note
