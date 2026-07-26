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
import sys
from collections.abc import Mapping
from pathlib import Path

from eee_agent.runtime.agent_context import PlainData

_log = logging.getLogger("eee_agent.sketch.chrome")

_MAX_HTML_BYTES = 128 * 1024
_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_RENDER_TIMEOUT_SECONDS = 30.0
_MAX_LOG_TAIL = 300

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
        except OSError:
            _log.exception("sketch html write failed")
            return _error("sketch.io_error", "Could not write the sketch HTML file.")

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

        if not png_path.is_file():
            tail = (stderr or b"").decode("utf-8", errors="replace")[-_MAX_LOG_TAIL:]
            _log.warning("headless render produced no PNG; stderr tail: %s", tail)
            return _error(
                "sketch.render_failed",
                "The headless browser produced no image. If the sketch loads "
                "Three.js from a CDN, check network access — offline renders "
                "come back blank.",
            )
        return {
            "ok": True,
            "image_path": str(png_path),
            "html_path": str(html_path),
            "image_bytes": png_path.stat().st_size,
        }


def _error(code: str, message: str) -> dict[str, PlainData]:
    return {"ok": False, "code": code, "message": message}


__all__ = ["ChromeSketchRenderer", "resolve_browser"]
