"""Anonymous headless-browser screenshot capture (design doc section 3, 方式C).

Feasibility was verified manually before this was written: an anonymous
Playwright context can open a TikTok LIVE viewer page and the <video>
element plays genuine, continuously-advancing live content (no login
redirect, no CAPTCHA). TikTok does show a recurring "log in to continue"
toast overlaid near the bottom of the player; rather than chase that toast's
unstable, undocumented DOM structure with click-to-dismiss selectors, the
capture clips it out by cropping the bottom margin of the video frame.
"""
import logging
import os
from datetime import datetime, timezone

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from . import proxy as proxy_module

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
VIEWPORT = {"width": 1280, "height": 900}
# How long to wait for the <video> element to appear. This used to be a flat
# 6s sleep followed by a presence check, which was enough on the Windows dev
# machine but NOT on the Linux VPS going out through a proxy: measured
# 2026-09-01, <video> was still absent at 6s and present at 8s, so every
# capture failed with "no <video> element found" even though the page loaded
# fine (HTTP 200, real live content, no CAPTCHA/login wall). Waiting on the
# selector instead of sleeping a fixed amount returns as soon as it appears
# and tolerates a slow host/proxy without slowing down a fast one.
VIDEO_WAIT_MS = 20000
# Once <video> exists it can still be showing an empty first frame, so give
# the stream a moment to paint before clipping.
FRAME_SETTLE_MS = 2500
# The "log in to continue" toast sits in roughly the bottom ~15% of the
# video element; crop it out rather than try to click-dismiss it.
BOTTOM_CROP_RATIO = 0.85


async def capture_live_screenshot(username: str, output_path: str, proxy_url: str | None = None) -> bool:
    """Best-effort: opens the LIVE viewer page anonymously and saves a
    screenshot of the video frame. Returns True on success, False on any
    failure (network issues, stream already ended, no <video> element,
    TikTok UI changes) — callers should treat this as non-fatal.

    proxy_url is optional (Phase 5 IP-based measurement prep) -- None
    connects with the real IP exactly as before this parameter existed;
    see proxy.py for the supported URL shape."""
    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    proxy_config = proxy_module.load_proxy_config(proxy_url)
    playwright_proxy = proxy_module.build_playwright_proxy(proxy_config)

    url = f"https://www.tiktok.com/@{username}/live"
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, proxy=playwright_proxy)
            try:
                context = await browser.new_context(viewport=VIEWPORT, user_agent=USER_AGENT)
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                try:
                    await page.wait_for_selector("video", timeout=VIDEO_WAIT_MS)
                except PlaywrightTimeoutError:
                    logger.warning(
                        "screenshot: no <video> element for @%s within %dms", username, VIDEO_WAIT_MS
                    )
                    return False
                await page.wait_for_timeout(FRAME_SETTLE_MS)

                video = page.locator("video").first

                box = await video.bounding_box()
                if box is None:
                    logger.warning("screenshot: <video> element has no bounding box for @%s", username)
                    return False

                clip = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"] * BOTTOM_CROP_RATIO,
                }
                await page.screenshot(path=output_path, clip=clip)
                logger.info("screenshot saved: %s", output_path)
                return True
            finally:
                await browser.close()
    except PlaywrightError as exc:
        logger.warning("screenshot capture failed for @%s: %s", username, exc)
        return False
    except Exception as exc:
        logger.warning("screenshot capture failed for @%s: %s", username, exc)
        return False


def default_screenshot_path(base_dir: str, live_session_id: int) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return os.path.join(base_dir, f"session{live_session_id}_{timestamp}.png")
