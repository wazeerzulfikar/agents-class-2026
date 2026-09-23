"""Find the best visual a student published for the week's build, or fall back to a capture.

Discovery is deterministic: the student's site root is opened in headless Chromium, links
that name the week lead to the post, and the visual elements rendered there (images, SVG
figures, canvases, videos) are measured and captured. The model only chooses among those
captures; platform code re-encodes the chosen one for email.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, ViewportSize, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from .models import CourseWeek
from .screenshots import ScreenshotError, SiteScreenshot, encode_jpeg

ImageKind = Literal["post_image", "screenshot"]
_MAX_STATIC_HTML_BYTES = 2 * 1024 * 1024
_VISUAL_TAGS = frozenset({"img", "svg", "canvas", "video"})
_ANCHORS_JS = (
    "() => Array.from(document.querySelectorAll('a[href]')).map(a => "
    "({href: a.getAttribute('href') || '', text: (a.innerText || a.textContent || "
    "a.getAttribute('aria-label') || '').trim().slice(0, 120)}))"
)
# Tags every visual element with its index so a chosen candidate can be captured later.
_VISUALS_JS = (
    "() => Array.from(document.querySelectorAll('img, svg, canvas, video')).map((el, i) => {"
    "  el.setAttribute('data-nl-idx', String(i));"
    "  if (el.tagName === 'VIDEO') { try { el.muted = true; el.preload = 'auto';"
    "    if (el.readyState < 2) { el.load(); } el.currentTime = 1; } catch (e) {} }"
    "  const r = el.getBoundingClientRect();"
    "  const style = window.getComputedStyle(el);"
    "  return {index: i, tag: el.tagName.toLowerCase(), width: Math.round(r.width),"
    "    height: Math.round(r.height), alt: (el.getAttribute('alt') || "
    "    el.getAttribute('aria-label') || el.getAttribute('title') || '').slice(0, 200),"
    "    src: el.currentSrc || el.src || '', visible: style.visibility !== 'hidden' && "
    "    style.display !== 'none' && Number(style.opacity || '1') > 0.05};"
    "})"
)


@dataclass(frozen=True)
class ImageCandidate:
    index: int
    tag: str
    width: int
    height: int
    alt: str
    src: str
    page_url: str

    @property
    def area(self) -> int:
        return self.width * self.height


@dataclass(frozen=True)
class FoundImage:
    shot: SiteScreenshot
    kind: ImageKind
    page_url: str
    source_url: str | None = None


class ImageJudge(Protocol):
    """Returns the 1-based index of the best capture, or 0 when none is suitable."""

    def __call__(self, *, prompt: str, images: Sequence[bytes]) -> int: ...


class HighlightImageFinder(Protocol):
    def find(self, *, site_url: str, week: CourseWeek, context: str) -> FoundImage | None: ...


def week_page_pattern(week_number: int) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![a-z0-9])(week|wk|w)[\s_./-]?0*{week_number}(?![0-9])",
        re.IGNORECASE,
    )


def discover_week_pages(
    anchors: Sequence[Mapping[str, object]],
    *,
    site_url: str,
    week: CourseWeek,
    limit: int = 3,
) -> list[str]:
    """Same-site links whose target or text names the week, in page order, root excluded."""

    return [
        page
        for page in discover_week_targets(anchors, site_url=site_url, week=week, limit=limit)
        if urlsplit(page).fragment == ""
    ]


def discover_week_anchor(
    anchors: Sequence[Mapping[str, object]],
    *,
    site_url: str,
    week: CourseWeek,
) -> str | None:
    """A same-page `#section` link naming the week, for single-page sites."""

    return next(
        (
            target
            for target in discover_week_targets(anchors, site_url=site_url, week=week, limit=10)
            if urlsplit(target).fragment
        ),
        None,
    )


def discover_week_targets(
    anchors: Sequence[Mapping[str, object]],
    *,
    site_url: str,
    week: CourseWeek,
    limit: int,
) -> list[str]:
    pattern = week_page_pattern(week.number)
    root_host = urlsplit(site_url).hostname
    root_page = urldefrag(site_url).url.rstrip("/")
    found: list[str] = []
    for anchor in anchors:
        href = anchor.get("href")
        text = anchor.get("text")
        if not isinstance(href, str):
            continue
        absolute = urljoin(site_url, href)
        page, fragment = urldefrag(absolute)
        parsed = urlsplit(page)
        if parsed.scheme != "https" or parsed.hostname != root_host:
            continue
        if not (pattern.search(href) or (isinstance(text, str) and pattern.search(text))):
            continue
        on_root = page.rstrip("/") == root_page
        if on_root and (not fragment or not pattern.search(fragment)):
            continue
        # Fragments matter only on the root page, where they name a week section.
        target = f"{page}#{fragment}" if on_root else page
        if target not in found:
            found.append(target)
            if len(found) >= limit:
                break
    return found


def static_anchors(site_url: str, *, timeout_seconds: float = 15.0) -> list[dict[str, object]]:
    """Links present in the served HTML, for sites whose scripts replace the DOM on load."""

    try:
        with httpx.Client(
            follow_redirects=True, max_redirects=5, timeout=timeout_seconds
        ) as client:
            response = client.get(site_url)
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").partition(";")[0].strip()
            if not media_type.casefold().startswith("text/html"):
                return []
            html = response.content[:_MAX_STATIC_HTML_BYTES].decode(
                response.encoding or "utf-8", errors="replace"
            )
    except httpx.HTTPError:
        return []
    soup = BeautifulSoup(html, "html.parser")
    anchors: list[dict[str, object]] = []
    for element in soup.select("a[href]"):
        href = element.get("href")
        if isinstance(href, str):
            anchors.append({"href": href, "text": element.get_text(" ", strip=True)[:120]})
    return anchors


def filter_candidates(
    raw_visuals: Sequence[Mapping[str, object]],
    *,
    page_url: str,
    min_width: int = 280,
    min_height: int = 140,
    limit: int = 6,
) -> list[ImageCandidate]:
    """Keep visible, reasonably sized visual elements, biggest first, one per source."""

    candidates: list[ImageCandidate] = []
    seen_sources: set[str] = set()
    for raw in raw_visuals:
        index = raw.get("index")
        tag = raw.get("tag")
        width = raw.get("width")
        height = raw.get("height")
        if (
            not isinstance(index, int)
            or not isinstance(tag, str)
            or tag not in _VISUAL_TAGS
            or not isinstance(width, int)
            or not isinstance(height, int)
            or raw.get("visible") is False
        ):
            continue
        if width < min_width or height < min_height:
            continue
        ratio = width / height
        if ratio < 0.4 or ratio > 4.0:
            continue
        src = raw.get("src")
        source = src if isinstance(src, str) else ""
        if source.startswith("data:"):
            source = ""
        if source and source in seen_sources:
            continue
        if source:
            seen_sources.add(source)
        alt = raw.get("alt")
        candidates.append(
            ImageCandidate(
                index=index,
                tag=tag,
                width=width,
                height=height,
                alt=alt if isinstance(alt, str) else "",
                src=source,
                page_url=page_url,
            )
        )
    return sorted(candidates, key=lambda item: -item.area)[:limit]


def build_judge_prompt(
    *, context: str, week: CourseWeek, candidates: Sequence[ImageCandidate]
) -> str:
    listing = "\n".join(
        f"{index}. {candidate.tag} {candidate.width}x{candidate.height}"
        + (f' alt="{candidate.alt}"' if candidate.alt else "")
        for index, candidate in enumerate(candidates, start=1)
    )
    return (
        "Choose the one image that best represents this student's build for a class "
        "newsletter highlight. Prefer a rendered result, demo, visualization, diagram, or "
        "artifact the student made for the assignment. Avoid logos, icons, portraits, stock "
        "decoration, blank or mostly empty frames, and images that are mostly small text. "
        "If none is suitable, answer 0.\n\n"
        f"Week {week.number} assignment: {week.tutorial}\n"
        f"Build: {context}\n\nCandidates:\n{listing}\n\n"
        'Respond with JSON only: {"choice": <number>, "reason": "<one sentence>"}'
    )


class PlaywrightImageFinder:
    """One headless Chromium session per build: discover the post, capture visuals, choose."""

    def __init__(
        self,
        *,
        judge: ImageJudge | None = None,
        executable_path: Path | None = None,
        viewport_width: int = 1200,
        viewport_height: int = 750,
        device_scale_factor: int = 2,
        output_width: int = 1200,
        timeout_ms: int = 20_000,
        settle_ms: int = 800,
        jpeg_quality: int = 84,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._judge = judge
        self._executable_path = executable_path
        self._viewport: ViewportSize = {"width": viewport_width, "height": viewport_height}
        self._device_scale_factor = device_scale_factor
        self._output_width = output_width
        self._timeout_ms = timeout_ms
        self._settle_ms = settle_ms
        self._jpeg_quality = jpeg_quality
        self._log = log or (lambda message: None)

    def find(self, *, site_url: str, week: CourseWeek, context: str) -> FoundImage | None:
        parsed = urlsplit(site_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ScreenshotError("Only public https sites are inspected.")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True,
                    executable_path=(
                        str(self._executable_path) if self._executable_path is not None else None
                    ),
                )
                try:
                    page = browser.new_page(
                        viewport=self._viewport,
                        device_scale_factor=self._device_scale_factor,
                    )
                    self._open(page, site_url)
                    rendered = page.evaluate(_ANCHORS_JS)
                    # Rendered links first, then the served HTML: single-page apps often
                    # replace the DOM and drop the plain links the template shipped with.
                    anchors = [
                        *(rendered if isinstance(rendered, list) else []),
                        *static_anchors(site_url),
                    ]
                    week_pages = discover_week_pages(anchors, site_url=site_url, week=week)
                    week_anchor = discover_week_anchor(anchors, site_url=site_url, week=week)
                    # Root visuals are considered only when the site has no week post; a
                    # landing-page graphic must not outrank a capture of the actual post.
                    scan = week_pages or [week_anchor or site_url]
                    for page_url in scan:
                        if page_url != site_url:
                            self._open(page, page_url)
                        found = self._capture_best(page, page_url, week=week, context=context)
                        if found is not None:
                            return found
                    fallback_url = scan[0]
                    self._open(page, fallback_url)
                    png = page.screenshot(type="png", full_page=False)
                finally:
                    browser.close()
        except (PlaywrightError, PlaywrightTimeoutError) as error:
            raise ScreenshotError(f"Site inspection failed ({type(error).__name__}).") from error
        return FoundImage(
            shot=encode_jpeg(png, max_width=self._output_width, quality=self._jpeg_quality),
            kind="screenshot",
            page_url=fallback_url,
        )

    def _open(self, page: Page, url: str) -> None:
        page.goto(url, wait_until="load", timeout=self._timeout_ms)
        with contextlib.suppress(PlaywrightTimeoutError):
            page.wait_for_load_state("networkidle", timeout=5_000)
        page.wait_for_timeout(self._settle_ms)

    def _capture_best(
        self, page: Page, page_url: str, *, week: CourseWeek, context: str
    ) -> FoundImage | None:
        raw_visuals = page.evaluate(_VISUALS_JS)
        if isinstance(raw_visuals, list) and any(
            isinstance(item, dict) and item.get("tag") == "video" for item in raw_visuals
        ):
            page.wait_for_timeout(self._settle_ms)  # let a nudged video paint a frame
        candidates = filter_candidates(
            raw_visuals if isinstance(raw_visuals, list) else [], page_url=page_url
        )
        captures: list[tuple[ImageCandidate, bytes]] = []
        for candidate in candidates:
            try:
                png = page.locator(f'[data-nl-idx="{candidate.index}"]').first.screenshot(
                    type="png", timeout=self._timeout_ms
                )
            except (PlaywrightError, PlaywrightTimeoutError) as error:
                self._log(
                    f"    {candidate.tag} #{candidate.index}: capture failed "
                    f"({type(error).__name__})"
                )
                continue
            captures.append((candidate, png))
        if not captures:
            return None
        chosen = self._choose(captures, week=week, context=context)
        if chosen is None:
            return None
        candidate, png = chosen
        try:
            shot = encode_jpeg(png, max_width=self._output_width, quality=self._jpeg_quality)
        except ScreenshotError as error:
            self._log(f"    {candidate.tag} #{candidate.index}: {error}")
            return None
        return FoundImage(
            shot=shot,
            kind="post_image",
            page_url=page_url,
            source_url=candidate.src or None,
        )

    def _choose(
        self,
        captures: Sequence[tuple[ImageCandidate, bytes]],
        *,
        week: CourseWeek,
        context: str,
    ) -> tuple[ImageCandidate, bytes] | None:
        if self._judge is None or len(captures) == 1:
            return captures[0]
        try:
            choice = self._judge(
                prompt=build_judge_prompt(
                    context=context,
                    week=week,
                    candidates=[candidate for candidate, _ in captures],
                ),
                images=[png for _, png in captures],
            )
        except Exception as error:  # the judge is advisory; fall back to the largest visual
            self._log(f"    image judge unavailable ({type(error).__name__}); using largest")
            return captures[0]
        if choice <= 0:
            return None
        return captures[min(choice, len(captures)) - 1]
