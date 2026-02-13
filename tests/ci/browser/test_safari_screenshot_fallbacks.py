"""Tests for Safari screenshot-event fallback behavior."""

import pytest

from browser_use.browser.events import ScreenshotEvent
from browser_use.browser.views import BrowserError, BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import SerializedDOMState
from safari_session import SafariBrowserSession


def _page_info() -> PageInfo:
	return PageInfo(
		viewport_width=1280,
		viewport_height=720,
		page_width=1280,
		page_height=720,
		scroll_x=0,
		scroll_y=0,
		pixels_above=0,
		pixels_below=0,
		pixels_left=0,
		pixels_right=0,
	)


@pytest.mark.asyncio
async def test_screenshot_event_uses_cached_screenshot_on_driver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	"""ScreenshotEvent should return cached screenshot when direct capture fails."""
	session = SafariBrowserSession()
	cached_screenshot = 'cached-screenshot'
	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={}),
		url='https://cached.example',
		title='Cached',
		tabs=[TabInfo(url='https://cached.example', title='Cached', target_id='cached-target', parent_target_id=None)],
		screenshot=cached_screenshot,
		page_info=_page_info(),
	)

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def failing_screenshot() -> str:
		raise RuntimeError('capture failed')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session.driver, 'screenshot', failing_screenshot)

	try:
		result = await session.on_ScreenshotEvent(ScreenshotEvent())
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert result == cached_screenshot
	assert 'screenshot_event_fallback:cached' in session._recent_events


@pytest.mark.asyncio
async def test_screenshot_event_raises_browser_error_without_cache(monkeypatch: pytest.MonkeyPatch) -> None:
	"""ScreenshotEvent should raise BrowserError when capture fails and no cached screenshot exists."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def failing_screenshot() -> str:
		raise RuntimeError('capture failed')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session.driver, 'screenshot', failing_screenshot)

	try:
		with pytest.raises(BrowserError, match='Safari screenshot failed'):
			await session.on_ScreenshotEvent(ScreenshotEvent())
	finally:
		await session.event_bus.stop(clear=True, timeout=5)
