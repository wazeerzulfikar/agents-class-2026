"""Capture bounded screenshots of deployed student sites for highlighted builds."""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urlsplit

from PIL import Image
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ScreenshotMediaType = Literal["image/jpeg", "image/png"]


class ScreenshotError(RuntimeError):
    """A site could not be captured; the newsletter continues without its image."""


@dataclass(frozen=True)
class SiteScreenshot:
    data: bytes
    media_type: ScreenshotMediaType
    width: int
    height: int


class SiteScreenshotter(Protocol):
    def __call__(self, url: str) -> SiteScreenshot: ...


def encode_jpeg(png: bytes, *, max_width: int, quality: int) -> SiteScreenshot:
    """Downscale a PNG capture to an email-sized progressive JPEG."""

    with Image.open(io.BytesIO(png)) as image:
        rgb = image.convert("RGB")
        if rgb.width > max_width:
            height = max(1, round(rgb.height * max_width / rgb.width))
            rgb = rgb.resize((max_width, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        return SiteScreenshot(
            data=buffer.getvalue(),
            media_type="image/jpeg",
            width=rgb.width,
            height=rgb.height,
        )


@dataclass(frozen=True)
class PlaywrightScreenshotter:
    """Headless Chromium capture of one public HTTPS page's first viewport."""

    executable_path: Path | None = None
    viewport_width: int = 1200
    viewport_height: int = 750
    output_width: int = 1200
    timeout_ms: int = 20_000
    settle_ms: int = 800
    jpeg_quality: int = 82

    def __call__(self, url: str) -> SiteScreenshot:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ScreenshotError("Only public https sites are captured.")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=(
                        str(self.executable_path) if self.executable_path is not None else None
                    ),
                )
                try:
                    page = browser.new_page(
                        viewport={"width": self.viewport_width, "height": self.viewport_height},
                        device_scale_factor=1,
                    )
                    page.goto(url, wait_until="load", timeout=self.timeout_ms)
                    with contextlib.suppress(PlaywrightTimeoutError):
                        page.wait_for_load_state("networkidle", timeout=5_000)
                    page.wait_for_timeout(self.settle_ms)
                    png = page.screenshot(type="png", full_page=False)
                finally:
                    browser.close()
        except (PlaywrightError, PlaywrightTimeoutError) as error:
            raise ScreenshotError(f"Screenshot failed ({type(error).__name__}).") from error
        return encode_jpeg(png, max_width=self.output_width, quality=self.jpeg_quality)
