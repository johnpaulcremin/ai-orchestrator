"""Automatic image-token cost reduction: downscaling and OCR-replacement
(app/image_processing.py), plus their wiring into run_orchestrator/
stream_orchestrator's primary and fallback call sites.
"""

from __future__ import annotations

import base64
import io
import logging

import pytest

from app import image_processing
from app import image_processing as imgproc

_PNG_1PX = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _make_png_data_url(width: int, height: int) -> str:
    from PIL import Image

    image = Image.new("RGB", (width, height), color=(255, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    b64 = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


@pytest.fixture(autouse=True)
def _reset_tesseract_cache():
    imgproc.reset_tesseract_cache()
    yield
    imgproc.reset_tesseract_cache()


# --- wants_fine_detail ---------------------------------------------------------


def test_wants_fine_detail_matches_known_phrases() -> None:
    assert imgproc.wants_fine_detail("Can you read the small text in this photo?")
    assert imgproc.wants_fine_detail("I need the EXACT WORDING here")
    assert imgproc.wants_fine_detail("this text is illegible, help")


def test_wants_fine_detail_false_for_ordinary_question() -> None:
    assert not imgproc.wants_fine_detail("what is in this image?")
    assert not imgproc.wants_fine_detail("")


# --- downscale_image_data_url ---------------------------------------------------


def test_downscale_leaves_small_image_unchanged() -> None:
    url = _make_png_data_url(50, 50)
    assert imgproc.downscale_image_data_url(url, max_dimension=1024) == url


def test_downscale_resizes_large_image() -> None:
    url = _make_png_data_url(2000, 1000)
    resized = imgproc.downscale_image_data_url(url, max_dimension=500)
    assert resized != url

    from PIL import Image

    _mime, raw = imgproc._decode_data_url(resized)
    with Image.open(io.BytesIO(raw)) as image:
        assert max(image.size) <= 500


def test_downscale_malformed_url_returns_unchanged() -> None:
    assert imgproc.downscale_image_data_url("not-a-data-url") == "not-a-data-url"


def test_downscale_corrupt_base64_image_bytes_fails_safe() -> None:
    # Valid data-url shape, but the "image" bytes aren't a real image — PIL
    # will raise on open(), which must be swallowed and the original returned.
    bogus = "data:image/png;base64," + base64.b64encode(b"not a png").decode("ascii")
    assert imgproc.downscale_image_data_url(bogus) == bogus


# --- ocr_extract / _tesseract_available -----------------------------------------


def test_ocr_extract_returns_none_when_tesseract_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_tesseract_available", lambda: False)
    assert imgproc.ocr_extract(_PNG_1PX) is None


def test_ocr_extract_returns_none_for_malformed_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_tesseract_available", lambda: True)
    assert imgproc.ocr_extract("not-a-data-url") is None


# --- process_images: gating and composition -------------------------------------


def test_process_images_no_images_is_noop() -> None:
    assert imgproc.process_images(None, "hi") == (None, None, None)
    assert imgproc.process_images([], "hi") == ([], None, None)


def test_process_images_skips_both_transforms_when_fine_detail_wanted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        imgproc, "downscale_image_data_url", lambda *a, **k: "SHOULD_NOT_BE_CALLED"
    )
    monkeypatch.setattr(imgproc, "ocr_extract", lambda *a, **k: ("text", 99.0))

    images = [_PNG_1PX]
    result = imgproc.process_images(images, "please read the small text exactly")
    assert result == (images, None, None)


def test_process_images_downscales_when_ocr_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_downscale_enabled", lambda: True)
    monkeypatch.setattr(imgproc, "_ocr_replacement_enabled", lambda: False)
    monkeypatch.setattr(
        imgproc, "downscale_image_data_url", lambda url, max_dimension=None: "RESIZED"
    )

    kept, appendix, note = imgproc.process_images([_PNG_1PX], "what is this")
    assert kept == ["RESIZED"]
    assert appendix is None
    assert note == "image_preprocessing: downscaled=1"


def test_process_images_replaces_with_ocr_text_when_confident(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_downscale_enabled", lambda: True)
    monkeypatch.setattr(imgproc, "_ocr_replacement_enabled", lambda: True)
    monkeypatch.setattr(
        imgproc,
        "ocr_extract",
        lambda url: ("a" * 45, 80.0),
    )
    monkeypatch.setattr(
        imgproc, "downscale_image_data_url", lambda *a, **k: pytest.fail("unreachable")
    )

    kept, appendix, note = imgproc.process_images([_PNG_1PX], "what does this say")
    assert kept is None
    assert appendix is not None
    assert "a" * 45 in appendix
    assert note == "image_preprocessing: ocr_replaced=1"


def test_process_images_keeps_image_when_ocr_confidence_too_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_downscale_enabled", lambda: True)
    monkeypatch.setattr(imgproc, "_ocr_replacement_enabled", lambda: True)
    monkeypatch.setattr(imgproc, "ocr_extract", lambda url: ("a" * 45, 10.0))
    monkeypatch.setattr(
        imgproc, "downscale_image_data_url", lambda url, max_dimension=None: url
    )

    kept, appendix, note = imgproc.process_images([_PNG_1PX], "what does this say")
    assert kept == [_PNG_1PX]
    assert appendix is None
    assert note is None


def test_process_images_keeps_image_when_ocr_text_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_downscale_enabled", lambda: True)
    monkeypatch.setattr(imgproc, "_ocr_replacement_enabled", lambda: True)
    monkeypatch.setattr(imgproc, "ocr_extract", lambda url: ("short", 90.0))
    monkeypatch.setattr(
        imgproc, "downscale_image_data_url", lambda url, max_dimension=None: url
    )

    kept, appendix, note = imgproc.process_images([_PNG_1PX], "what does this say")
    assert kept == [_PNG_1PX]
    assert appendix is None
    assert note is None


def test_process_images_both_transforms_disabled_returns_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imgproc, "_downscale_enabled", lambda: False)
    monkeypatch.setattr(imgproc, "_ocr_replacement_enabled", lambda: False)

    images = [_PNG_1PX]
    kept, appendix, note = imgproc.process_images(images, "what is this")
    assert kept == images
    assert appendix is None
    assert note is None


# --- startup warning: OCR_REPLACEMENT on with no Tesseract --------------------


def test_warns_when_ocr_is_enabled_but_tesseract_is_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Enabled and unusable, in silence: _tesseract_available() returns False,
    caches it for the process, and every ocr_extract() returns None with no
    log line — while self_describe still reports OCR_REPLACEMENT as an
    enabled feature. Same shape as the unreachable-local-model warning."""
    from app import main

    monkeypatch.setenv("OCR_REPLACEMENT", "true")
    monkeypatch.delenv("TESSERACT_CMD", raising=False)
    monkeypatch.setattr(main, "get_model_overrides", lambda: {})
    monkeypatch.setattr(image_processing, "tesseract_available", lambda: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_ocr_unavailable()

    message = caplog.text
    assert "startup.ocr_unavailable" in message
    assert "OCR_REPLACEMENT is ON" in message
    assert "does nothing at all" in message  # the consequence, stated outright
    assert "install Tesseract" in message


def test_ocr_warning_names_a_configured_tesseract_cmd_that_does_not_work(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app import main

    monkeypatch.setenv("OCR_REPLACEMENT", "true")
    monkeypatch.setenv("TESSERACT_CMD", "/wrong/path/tesseract")
    monkeypatch.setattr(image_processing, "tesseract_available", lambda: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_ocr_unavailable()

    assert "/wrong/path/tesseract" in caplog.text
    assert "install Tesseract" not in caplog.text


def test_no_ocr_warning_on_the_default_because_it_defaults_to_on(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """OCR_REPLACEMENT defaults to ON and Tesseract is an optional system
    binary most installs lack — warning on the DEFAULT would fire on the
    majority of fresh installs about a graceful degradation nobody asked
    for, which is how a real warning gets ignored."""
    from app import main

    monkeypatch.delenv("OCR_REPLACEMENT", raising=False)
    monkeypatch.setattr(main, "get_model_overrides", lambda: {})
    monkeypatch.setattr(image_processing, "tesseract_available", lambda: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_ocr_unavailable()

    assert "startup.ocr_unavailable" not in caplog.text


def test_ocr_warning_fires_for_a_saved_settings_override_too(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Explicit means explicit by either route — env var or the Settings
    panel, the same override > env > default chain every flag uses."""
    from app import main

    monkeypatch.delenv("OCR_REPLACEMENT", raising=False)
    monkeypatch.setattr(
        main, "get_model_overrides", lambda: {"OCR_REPLACEMENT": "true"}
    )
    monkeypatch.setattr(image_processing, "tesseract_available", lambda: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_ocr_unavailable()

    assert "startup.ocr_unavailable" in caplog.text


def test_no_ocr_warning_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing to warn about: the owner never asked for it."""
    from app import main

    monkeypatch.delenv("OCR_REPLACEMENT", raising=False)
    monkeypatch.setattr(image_processing, "tesseract_available", lambda: False)

    with caplog.at_level(logging.WARNING):
        main._warn_if_ocr_unavailable()

    assert "startup.ocr_unavailable" not in caplog.text


def test_no_ocr_warning_when_tesseract_is_present(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from app import main

    monkeypatch.setenv("OCR_REPLACEMENT", "true")
    monkeypatch.setattr(image_processing, "tesseract_available", lambda: True)

    with caplog.at_level(logging.WARNING):
        main._warn_if_ocr_unavailable()

    assert "startup.ocr_unavailable" not in caplog.text
