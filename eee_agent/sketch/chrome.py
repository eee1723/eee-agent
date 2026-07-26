"""Headless-Chrome sketch renderer behind the SketchRenderProvider seam.

Deterministic and self-contained: no Houdini, no bridge, no network of its
own (the HTML itself may pull Three.js from a CDN — offline renders come
back blank, which the caller surfaces honestly). The browser is spawned
with a fixed argument list; the only variable inputs are the bounded,
sanitized sketch name and the HTML payload written under ``output_dir``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import struct
import sys
import zlib
from collections.abc import Mapping
from pathlib import Path

from eee_agent.runtime.agent_context import PlainData

_log = logging.getLogger("eee_agent.sketch.chrome")

_MAX_HTML_BYTES = 128 * 1024
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_RENDER_TIMEOUT_SECONDS = 30.0
_MAX_LOG_TAIL = 300
_MAX_PNG_BYTES = 16 * 1024 * 1024
_MAX_DECODED_BYTES = 64 * 1024 * 1024
_MIN_IMAGE_WIDTH = 320
_MIN_IMAGE_HEIGHT = 200
_MIN_CHANNEL_SPAN = 8

# Fixed headless flags. Nothing model-controlled ever enters this list.
_HEADLESS_ARGS = (
    "--headless=new",
    "--disable-gpu",
    "--use-angle=swiftshader",
    "--window-size=1440,900",
    "--virtual-time-budget=8000",
    "--no-first-run",
    "--disable-extensions",
)


def _chrome_candidates() -> list[str]:
    override = os.getenv("EEE_CHROME_PATH")
    candidates: list[str] = [override] if override else []
    if sys.platform.startswith("win"):
        candidates += [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:
        candidates += ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"]
    return candidates


def resolve_browser() -> str | None:
    """Return the first usable browser executable, or None."""
    for candidate in _chrome_candidates():
        if not candidate:
            continue
        if os.sep in candidate or (os.altsep and os.altsep in candidate):
            if Path(candidate).is_file():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


class ChromeSketchRenderer:
    """SketchRenderProvider implementation using headless Chrome/Edge."""

    def __init__(self, output_dir: Path) -> None:
        self._output_dir = Path(output_dir)

    async def render_sketch(
        self, *, html_content: str, sketch_name: str
    ) -> Mapping[str, PlainData]:
        if type(html_content) is not str or not html_content:
            return _error("sketch.input_invalid", "html_content must be a non-empty string.")
        if len(html_content.encode("utf-8", errors="replace")) > _MAX_HTML_BYTES:
            return _error("sketch.input_invalid", "html_content exceeds the 128KB bound.")
        if type(sketch_name) is not str or not _NAME_RE.match(sketch_name):
            return _error(
                "sketch.input_invalid",
                "sketch_name must match [a-z0-9_-]{1,64}.",
            )

        browser = resolve_browser()
        if browser is None:
            return _error(
                "sketch.browser_missing",
                "No Chrome/Edge executable found. Set EEE_CHROME_PATH to the "
                "browser binary and retry.",
            )

        self._output_dir.mkdir(parents=True, exist_ok=True)
        html_path = self._output_dir / f"{sketch_name}.html"
        png_path = self._output_dir / f"{sketch_name}.png"
        try:
            html_path.write_text(html_content, encoding="utf-8")
            # A failed browser invocation must never inherit a same-name PNG
            # from an earlier successful render.
            png_path.unlink(missing_ok=True)
        except OSError:
            _log.exception("sketch input preparation failed")
            return _error("sketch.io_error", "Could not prepare the sketch files.")

        cmd = [
            browser,
            *_HEADLESS_ARGS,
            f"--screenshot={png_path}",
            html_path.resolve().as_uri(),
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_RENDER_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return _error(
                    "sketch.render_timeout",
                    "Headless browser render timed out after 30s.",
                )
        except OSError:
            _log.exception("headless browser spawn failed")
            return _error("sketch.render_failed", "Could not launch the headless browser.")

        if proc.returncode not in (None, 0):
            tail = (stderr or b"").decode("utf-8", errors="replace")[-_MAX_LOG_TAIL:]
            _log.warning(
                "headless render exited non-zero (%s); stderr tail: %s",
                proc.returncode,
                tail,
            )
            return _error(
                "sketch.render_failed",
                "The headless browser exited before producing a valid image.",
            )
        if not png_path.is_file():
            tail = (stderr or b"").decode("utf-8", errors="replace")[-_MAX_LOG_TAIL:]
            _log.warning("headless render produced no PNG; stderr tail: %s", tail)
            return _error(
                "sketch.render_failed",
                "The headless browser produced no image. If the sketch loads "
                "Three.js from a CDN, check network access — offline renders "
                "come back blank.",
            )
        inspection = _inspect_png(png_path)
        if inspection.get("ok") is not True:
            return _error(
                str(inspection.get("code", "sketch.image_invalid")),
                str(
                    inspection.get(
                        "message", "The headless browser produced an invalid image."
                    )
                ),
            )
        return {
            "ok": True,
            "image_path": str(png_path),
            "html_path": str(html_path),
            "image_bytes": png_path.stat().st_size,
            "image_width": inspection["width"],
            "image_height": inspection["height"],
            "pixel_channel_span": inspection["channel_span"],
        }


def _inspect_png(path: Path) -> dict[str, PlainData]:
    """Strictly validate a bounded 8-bit RGB/RGBA PNG and reject blank output."""
    try:
        size = path.stat().st_size
        if not 0 < size <= _MAX_PNG_BYTES:
            return _error(
                "sketch.image_invalid",
                "Rendered PNG is empty or exceeds the 16MB bound.",
            )
        raw = path.read_bytes()
    except OSError:
        return _error("sketch.io_error", "Could not read the rendered PNG.")
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return _error("sketch.image_invalid", "Rendered file is not a PNG image.")

    offset = 8
    ihdr: bytes | None = None
    idat = bytearray()
    seen_iend = False
    while offset + 12 <= len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if length > _MAX_PNG_BYTES or crc_end > len(raw):
            return _error("sketch.image_invalid", "Rendered PNG has invalid chunks.")
        data = raw[data_start:data_end]
        expected_crc = struct.unpack(">I", raw[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(data, actual_crc) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            return _error("sketch.image_invalid", "Rendered PNG checksum is invalid.")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                return _error("sketch.image_invalid", "Rendered PNG header is invalid.")
            ihdr = data
        elif chunk_type == b"IDAT":
            idat.extend(data)
            if len(idat) > _MAX_PNG_BYTES:
                return _error("sketch.image_invalid", "Rendered PNG data is too large.")
        elif chunk_type == b"IEND":
            seen_iend = True
            offset = crc_end
            break
        offset = crc_end
    if ihdr is None or not idat or not seen_iend or offset != len(raw):
        return _error("sketch.image_invalid", "Rendered PNG is incomplete.")

    width, height, bit_depth, color_type, compression, filter_method, interlace = (
        struct.unpack(">IIBBBBB", ihdr)
    )
    if (
        width < _MIN_IMAGE_WIDTH
        or height < _MIN_IMAGE_HEIGHT
        or width > 8192
        or height > 8192
    ):
        return _error(
            "sketch.image_invalid",
            "Rendered PNG dimensions are outside the accepted range.",
        )
    channels = {2: 3, 6: 4}.get(color_type)
    if (
        bit_depth != 8
        or channels is None
        or compression != 0
        or filter_method != 0
        or interlace != 0
    ):
        return _error(
            "sketch.image_invalid",
            "Rendered PNG uses an unsupported pixel format.",
        )
    stride = width * channels
    expected_size = (stride + 1) * height
    if expected_size > _MAX_DECODED_BYTES:
        return _error("sketch.image_invalid", "Rendered PNG is too large to inspect.")
    try:
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(bytes(idat), expected_size + 1)
    except zlib.error:
        return _error("sketch.image_invalid", "Rendered PNG pixels are corrupt.")
    if (
        len(decoded) != expected_size
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        return _error("sketch.image_invalid", "Rendered PNG pixel size is invalid.")

    previous = bytearray(stride)
    rgb_min = [255, 255, 255]
    rgb_max = [0, 0, 0]
    visible_pixels = 0
    cursor = 0
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        scanline = bytearray(decoded[cursor : cursor + stride])
        cursor += stride
        if filter_type > 4:
            return _error("sketch.image_invalid", "Rendered PNG filter is invalid.")
        for index in range(stride):
            left = scanline[index - channels] if index >= channels else 0
            up = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            if filter_type == 1:
                scanline[index] = (scanline[index] + left) & 0xFF
            elif filter_type == 2:
                scanline[index] = (scanline[index] + up) & 0xFF
            elif filter_type == 3:
                scanline[index] = (scanline[index] + ((left + up) // 2)) & 0xFF
            elif filter_type == 4:
                scanline[index] = (
                    scanline[index] + _paeth(left, up, upper_left)
                ) & 0xFF
        for index in range(0, stride, channels):
            alpha = scanline[index + 3] if channels == 4 else 255
            if alpha == 0:
                continue
            visible_pixels += 1
            for channel in range(3):
                value = scanline[index + channel]
                rgb_min[channel] = min(rgb_min[channel], value)
                rgb_max[channel] = max(rgb_max[channel], value)
        previous = scanline

    if visible_pixels < max(1, width * height // 100):
        return _error(
            "sketch.image_blank",
            "Rendered PNG is fully or almost fully transparent.",
        )
    channel_span = max(
        rgb_max[channel] - rgb_min[channel] for channel in range(3)
    )
    if channel_span < _MIN_CHANNEL_SPAN:
        return _error(
            "sketch.image_blank",
            "Rendered PNG is blank or effectively a single flat color.",
        )
    return {
        "ok": True,
        "width": width,
        "height": height,
        "channel_span": channel_span,
    }


def _paeth(left: int, up: int, upper_left: int) -> int:
    estimate = left + up - upper_left
    left_distance = abs(estimate - left)
    up_distance = abs(estimate - up)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= up_distance and left_distance <= upper_left_distance:
        return left
    if up_distance <= upper_left_distance:
        return up
    return upper_left


def _error(code: str, message: str) -> dict[str, PlainData]:
    return {"ok": False, "code": code, "message": message}


__all__ = ["ChromeSketchRenderer", "resolve_browser"]
