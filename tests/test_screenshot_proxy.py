import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tiktok_monitor.screenshot import capture_live_screenshot

# Exercises capture_live_screenshot's proxy wiring against a fully mocked
# Playwright chain (async_playwright() -> p.chromium.launch(...) -> ...) so
# no real browser is launched. The mocked <video> locator reports count()=0,
# which makes capture_live_screenshot return False right after the launch()
# call -- exactly enough of the flow to inspect what proxy= was passed,
# without needing to fake bounding_box()/screenshot() too.


def make_mocked_playwright():
    """Returns (async_playwright_patch_target_value, mock_launch) -- the
    mock async_playwright() context manager and the mock chromium.launch."""
    video_locator = MagicMock()
    video_locator.count = AsyncMock(return_value=0)

    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.locator.return_value.first = video_locator

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    mock_launch = AsyncMock(return_value=browser)
    chromium = MagicMock()
    chromium.launch = mock_launch

    p = MagicMock()
    p.chromium = chromium

    playwright_cm = MagicMock()
    playwright_cm.__aenter__ = AsyncMock(return_value=p)
    playwright_cm.__aexit__ = AsyncMock(return_value=False)

    return playwright_cm, mock_launch


@patch("tiktok_monitor.screenshot.async_playwright")
def test_capture_passes_no_proxy_when_unconfigured(mock_async_playwright):
    playwright_cm, mock_launch = make_mocked_playwright()
    mock_async_playwright.return_value = playwright_cm

    result = asyncio.run(capture_live_screenshot("some_streamer", "C:/tmp/does_not_matter.png"))

    assert result is False  # no <video> element -- expected given the mock
    mock_launch.assert_awaited_once_with(headless=True, proxy=None)


@patch("tiktok_monitor.screenshot.async_playwright")
def test_capture_passes_playwright_proxy_dict_when_configured(mock_async_playwright):
    playwright_cm, mock_launch = make_mocked_playwright()
    mock_async_playwright.return_value = playwright_cm

    asyncio.run(
        capture_live_screenshot(
            "some_streamer",
            "C:/tmp/does_not_matter.png",
            proxy_url="http://user:pass@proxy.example.com:8080",
        )
    )

    mock_launch.assert_awaited_once_with(
        headless=True,
        proxy={"server": "http://proxy.example.com:8080", "username": "user", "password": "pass"},
    )


@patch("tiktok_monitor.screenshot.async_playwright")
def test_capture_rejects_unsupported_proxy_scheme(mock_async_playwright):
    playwright_cm, mock_launch = make_mocked_playwright()
    mock_async_playwright.return_value = playwright_cm

    try:
        asyncio.run(
            capture_live_screenshot(
                "some_streamer", "C:/tmp/does_not_matter.png", proxy_url="socks5://proxy.example.com:1080"
            )
        )
        assert False, "expected ValueError for an unsupported proxy scheme"
    except ValueError as exc:
        assert "Unsupported proxy scheme" in str(exc)
    mock_launch.assert_not_awaited()  # failed before ever reaching Playwright
