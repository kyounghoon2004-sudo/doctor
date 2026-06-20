"""Playwright-based scraper for YouTube Shorts *and* regular videos.

Responsibilities:
  * Collect video URLs from a search query or a channel feed — for either
    Shorts (/shorts/<id>) or regular videos (/watch?v=<id>).
  * Keep scrolling until the requested count is reached, the results run out,
    or a safety cap is hit (persistent collection).
  * Navigate to an individual video and capture a clean screenshot of the
    player frame (works for both Shorts and regular watch pages).
  * Insert randomized, human-like delays between actions.

No login/authentication is used or required: these pages are publicly viewable,
so the scraper only reads anonymous pages.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import quote_plus

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

import config

logger = logging.getLogger(__name__)

_ID = r"([A-Za-z0-9_-]{11})"
_SHORTS_ID_RE = re.compile(r"/shorts/" + _ID)
_WATCH_ID_RE = re.compile(r"[?&]v=" + _ID)
_SHORT_LINK_RE = re.compile(r"youtu\.be/" + _ID)
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9_-]")

# Content types
SHORTS = "shorts"
VIDEOS = "videos"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_video_id(url: str) -> str | None:
    """Return the 11-char video id from any YouTube URL form, or None."""
    for pattern in (_SHORTS_ID_RE, _WATCH_ID_RE, _SHORT_LINK_RE):
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def is_short_url(url: str) -> bool:
    return "/shorts/" in (url or "")


def sanitize_filename(name: str) -> str:
    """Make an arbitrary string safe to use as a filename stem."""
    cleaned = _UNSAFE_FILENAME_RE.sub("_", name).strip("_")
    return cleaned or "video"


def canonical_url(video_id: str, content_type: str = SHORTS) -> str:
    """Build a canonical watch URL for a video id and content type."""
    if content_type == VIDEOS:
        return f"https://www.youtube.com/watch?v={video_id}"
    return f"https://www.youtube.com/shorts/{video_id}"


async def human_delay(
    min_s: float = config.MIN_DELAY, max_s: float = config.MAX_DELAY
) -> None:
    """Sleep a random amount of time to mimic human browsing cadence."""
    delay = random.uniform(min_s, max_s)
    logger.debug("Human delay: %.2fs", delay)
    await asyncio.sleep(delay)


def build_search_url(query: str, content_type: str = SHORTS) -> str:
    """Construct a YouTube search URL for a natural-language query.

    For Shorts we add the ``sp=EgIYAQ%3D%3D`` filter (Type: Shorts). For regular
    videos we use a plain search and collect /watch?v= links (which excludes
    Shorts), avoiding any dependency on a specific filter code.
    """
    base = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
    if content_type == SHORTS:
        return base + "&sp=EgIYAQ%3D%3D"
    return base


def build_channel_url(channel: str, content_type: str = SHORTS) -> str:
    """Construct a channel's Shorts or Videos tab URL."""
    handle = channel if channel.startswith("@") else f"@{channel}"
    tab = "shorts" if content_type == SHORTS else "videos"
    return f"https://www.youtube.com/{handle}/{tab}"


def _link_selector(content_type: str) -> str:
    return 'a[href*="/shorts/"]' if content_type == SHORTS else 'a[href*="/watch?v="]'


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
class ShortsScraper:
    """Async context manager that owns a Playwright browser session.

    Handles both Shorts and regular videos.

    Usage:
        async with ShortsScraper() as scraper:
            urls = await scraper.collect_from_search("AI news", 10, "videos")
            path = await scraper.capture_screenshot(urls[0])
    """

    def __init__(self) -> None:
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None

    async def __aenter__(self) -> "ShortsScraper":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        """Launch the browser and create a context."""
        logger.info("Launching browser (headless=%s)...", config.HEADLESS)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=config.HEADLESS,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent=config.USER_AGENT,
            viewport=config.VIEWPORT,
            locale=config.LOCALE,
            timezone_id=config.TIMEZONE,
        )
        self._context.set_default_navigation_timeout(config.NAVIGATION_TIMEOUT_MS)
        self._context.set_default_timeout(config.NAVIGATION_TIMEOUT_MS)

    async def close(self) -> None:
        """Tear down the browser session, swallowing shutdown errors."""
        for closer in (
            getattr(self._context, "close", None),
            getattr(self._browser, "close", None),
        ):
            if closer is not None:
                try:
                    await closer()
                except Exception as exc:  # pragma: no cover - best effort cleanup
                    logger.debug("Error during browser teardown: %s", exc)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("Error stopping Playwright: %s", exc)
        self._context = self._browser = self._playwright = None

    # -- internal -----------------------------------------------------------
    async def _new_page(self) -> Page:
        if self._context is None:
            raise RuntimeError("Scraper not started. Use 'async with' or call start().")
        return await self._context.new_page()

    @staticmethod
    async def _dismiss_consent(page: Page) -> None:
        """Best-effort click on the EU cookie/consent dialog if present."""
        selectors = [
            'button[aria-label*="Accept all" i]',
            'button[aria-label*="Reject all" i]',
            'button:has-text("Accept all")',
            'button:has-text("I agree")',
            'tp-yt-paper-button:has-text("Accept all")',
        ]
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if await button.count() > 0 and await button.is_visible():
                    await button.click(timeout=4_000)
                    logger.debug("Dismissed consent dialog via %s", selector)
                    await page.wait_for_timeout(1_000)
                    return
            except (PlaywrightError, PlaywrightTimeoutError):
                continue

    # -- public API ---------------------------------------------------------
    async def collect_urls(
        self,
        target_url: str,
        max_count: int = config.MAX_SHORTS_PER_TARGET,
        content_type: str = SHORTS,
    ) -> list[str]:
        """Scroll a feed/search page until ``max_count`` URLs are collected.

        Keeps scrolling past the lazy-load boundary; stops only when the count
        is reached, no new items appear for SCROLL_STALL_LIMIT consecutive
        scrolls, or MAX_SCROLLS_HARD_CAP is hit.
        """
        kind = "Shorts" if content_type == SHORTS else "videos"
        logger.info("Collecting up to %d %s from: %s", max_count, kind, target_url)
        selector = _link_selector(content_type)
        page = await self._new_page()
        collected: dict[str, str] = {}  # video_id -> url (dedup, keep order)
        try:
            await page.goto(target_url, wait_until="domcontentloaded")
            await self._dismiss_consent(page)
            await page.wait_for_timeout(2_000)

            stalls = 0
            scrolls = 0
            while True:
                hrefs = await page.eval_on_selector_all(
                    selector, "els => els.map(e => e.href)"
                )
                before = len(collected)
                for href in hrefs:
                    vid = extract_video_id(href)
                    if vid and vid not in collected:
                        collected[vid] = canonical_url(vid, content_type)

                new_found = len(collected) - before
                logger.info(
                    "  scroll %d -> %d/%d collected (+%d)",
                    scrolls,
                    len(collected),
                    max_count,
                    new_found,
                )

                if len(collected) >= max_count:
                    break
                if scrolls >= config.MAX_SCROLLS_HARD_CAP:
                    logger.warning("Hit scroll cap (%d); stopping.", config.MAX_SCROLLS_HARD_CAP)
                    break

                stalls = stalls + 1 if new_found == 0 else 0
                if stalls >= config.SCROLL_STALL_LIMIT:
                    logger.info("No new results after %d scrolls; results exhausted.", stalls)
                    break

                await page.mouse.wheel(0, 5_000)
                await human_delay()
                scrolls += 1

            urls = list(collected.values())[:max_count]
            logger.info("Collected %d %s URL(s).", len(urls), kind)
            return urls
        except PlaywrightTimeoutError:
            logger.error("Timed out loading feed: %s", target_url)
            return list(collected.values())[:max_count]
        except PlaywrightError as exc:
            logger.error("Browser error while collecting URLs: %s", exc)
            return list(collected.values())[:max_count]
        finally:
            await page.close()

    async def collect_from_channel(
        self,
        channel: str,
        max_count: int = config.MAX_SHORTS_PER_TARGET,
        content_type: str = SHORTS,
    ) -> list[str]:
        return await self.collect_urls(
            build_channel_url(channel, content_type), max_count, content_type
        )

    async def collect_from_search(
        self,
        query: str,
        max_count: int = config.MAX_SHORTS_PER_TARGET,
        content_type: str = SHORTS,
    ) -> list[str]:
        """Find Shorts or videos matching a natural-language query."""
        return await self.collect_urls(
            build_search_url(query, content_type), max_count, content_type
        )

    async def capture_screenshot(self, url: str) -> Path | None:
        """Navigate to a Short or video and screenshot its player frame.

        Returns the path to the saved PNG, or None on failure.
        """
        video_id = extract_video_id(url) or sanitize_filename(url)
        out_path = config.SCREENSHOTS_DIR / f"{sanitize_filename(video_id)}.png"
        short = is_short_url(url)
        logger.info("Capturing screenshot for %s", url)

        # Player container selectors differ between Shorts and watch pages.
        selectors = (
            ("#shorts-player", "ytd-reel-video-renderer", "video")
            if short
            else ("#movie_player", "video")
        )

        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await self._dismiss_consent(page)

            try:
                await page.wait_for_selector("video", timeout=config.NAVIGATION_TIMEOUT_MS)
            except PlaywrightTimeoutError:
                logger.warning("No <video> element appeared for %s", url)
            await page.wait_for_timeout(int(config.SCREENSHOT_RENDER_PAUSE_S * 1000))

            for selector in selectors:
                element = page.locator(selector).first
                try:
                    if await element.count() > 0 and await element.is_visible():
                        await element.screenshot(path=str(out_path))
                        logger.info("Saved screenshot -> %s", out_path.name)
                        return out_path
                except (PlaywrightError, PlaywrightTimeoutError) as exc:
                    logger.debug("Selector %s screenshot failed: %s", selector, exc)
                    continue

            await page.screenshot(path=str(out_path))
            logger.info("Saved full-page screenshot -> %s", out_path.name)
            return out_path
        except PlaywrightTimeoutError:
            logger.error("Timed out loading %s", url)
            return None
        except PlaywrightError as exc:
            logger.error("Browser error capturing %s: %s", url, exc)
            return None
        finally:
            await page.close()


# ---------------------------------------------------------------------------
# Convenience helpers for callers that only have a list of URLs
# ---------------------------------------------------------------------------
def normalize_url_list(raw_urls: Iterable[str]) -> list[str]:
    """Canonicalize and deduplicate user-supplied URLs (Shorts or videos)."""
    seen: dict[str, str] = {}
    for raw in raw_urls:
        url = raw.strip()
        if not url:
            continue
        vid = extract_video_id(url)
        if vid:
            ctype = SHORTS if is_short_url(url) else VIDEOS
            seen.setdefault(vid, canonical_url(vid, ctype))
        else:
            seen.setdefault(url, url)
    return list(seen.values())
