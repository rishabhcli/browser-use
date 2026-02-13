"""Tests for Safari BrowserStateRequestEvent fallback behavior."""

import pytest

from browser_use.browser.events import BrowserStateRequestEvent
from browser_use.browser.views import BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import SerializedDOMState
from safari_session import SafariBrowserSession


def _make_page_info(*, width: int, height: int, scroll_y: int = 0, page_height: int | None = None) -> PageInfo:
	"""Construct a consistent PageInfo object for tests."""
	total_height = page_height if page_height is not None else height
	pixels_below = max(total_height - (scroll_y + height), 0)
	return PageInfo(
		viewport_width=width,
		viewport_height=height,
		page_width=width,
		page_height=total_height,
		scroll_x=0,
		scroll_y=scroll_y,
		pixels_above=scroll_y,
		pixels_below=pixels_below,
		pixels_left=0,
		pixels_right=0,
	)


@pytest.mark.asyncio
async def test_browser_state_request_uses_cached_dom_when_dom_extraction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	"""If DOM extraction fails, Safari session should fall back to the cached DOM state."""
	session = SafariBrowserSession()
	cached_dom = SerializedDOMState(_root=None, selector_map={})
	cached_page_info = _make_page_info(width=900, height=600)
	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=cached_dom,
		url='https://cached.example',
		title='Cached',
		tabs=[TabInfo(url='https://cached.example', title='Cached', target_id='cached-target', parent_target_id=None)],
		screenshot=None,
		page_info=cached_page_info,
	)
	live_page_info = _make_page_info(width=1280, height=720, scroll_y=50, page_height=1800)
	live_tabs = [TabInfo(url='https://live.example', title='Live', target_id='live-target', parent_target_id=None)]

	async def fake_start(self: SafariBrowserSession) -> None:
		del self
		return None

	async def fake_refresh_download_tracking(
		self: SafariBrowserSession, wait_for_new: bool = False, timeout_seconds: float = 1.5
	) -> list[str]:
		del self, wait_for_new, timeout_seconds
		return []

	async def fake_refresh_tabs(self: SafariBrowserSession) -> list[TabInfo]:
		del self
		return live_tabs

	async def fake_rebuild_interactive_dom_state(self: SafariBrowserSession) -> SerializedDOMState:
		del self
		raise RuntimeError('dom exploded')

	async def fake_compute_page_info(self: SafariBrowserSession) -> PageInfo:
		del self
		return live_page_info

	async def fake_get_url() -> str:
		return 'https://live.example'

	async def fake_get_title() -> str:
		return 'Live'

	async def fake_screenshot() -> str:
		return 'abc123'

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(SafariBrowserSession, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(SafariBrowserSession, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(SafariBrowserSession, '_rebuild_interactive_dom_state', fake_rebuild_interactive_dom_state)
	monkeypatch.setattr(SafariBrowserSession, '_compute_page_info', fake_compute_page_info)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session.driver, 'screenshot', fake_screenshot)

	try:
		summary = await session.on_BrowserStateRequestEvent(
			BrowserStateRequestEvent(include_dom=True, include_screenshot=True, include_recent_events=True)
		)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.dom_state is cached_dom
	assert summary.url == 'https://live.example'
	assert summary.title == 'Live'
	assert summary.page_info == live_page_info
	assert summary.screenshot == 'abc123'
	assert any('Safari DOM extraction failed' in error for error in summary.browser_errors)
	assert summary.recent_events is not None
	assert 'dom_fallback:cached_or_empty' in summary.recent_events


@pytest.mark.asyncio
async def test_browser_state_request_falls_back_when_metrics_and_screenshot_fail(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""When screenshot/page metrics fail, Safari session should return a usable fallback summary."""
	session = SafariBrowserSession()
	live_tabs = [TabInfo(url='https://live.example', title='Live', target_id='live-target', parent_target_id=None)]
	live_dom = SerializedDOMState(_root=None, selector_map={})

	async def fake_start(self: SafariBrowserSession) -> None:
		del self
		return None

	async def fake_refresh_download_tracking(
		self: SafariBrowserSession, wait_for_new: bool = False, timeout_seconds: float = 1.5
	) -> list[str]:
		del self, wait_for_new, timeout_seconds
		return []

	async def fake_refresh_tabs(self: SafariBrowserSession) -> list[TabInfo]:
		del self
		return live_tabs

	async def fake_rebuild_interactive_dom_state(self: SafariBrowserSession) -> SerializedDOMState:
		del self
		return live_dom

	async def fake_compute_page_info(self: SafariBrowserSession) -> PageInfo:
		del self
		raise RuntimeError('page metrics exploded')

	async def fake_get_url() -> str:
		return 'https://live.example'

	async def fake_get_title() -> str:
		return 'Live'

	async def fake_screenshot() -> str:
		raise RuntimeError('screenshot exploded')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(SafariBrowserSession, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(SafariBrowserSession, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(SafariBrowserSession, '_rebuild_interactive_dom_state', fake_rebuild_interactive_dom_state)
	monkeypatch.setattr(SafariBrowserSession, '_compute_page_info', fake_compute_page_info)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session.driver, 'screenshot', fake_screenshot)

	try:
		summary = await session.on_BrowserStateRequestEvent(
			BrowserStateRequestEvent(include_dom=True, include_screenshot=True, include_recent_events=False)
		)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.dom_state is live_dom
	assert summary.screenshot is None
	assert summary.page_info is not None
	assert summary.page_info.viewport_width == 0
	assert summary.page_info.viewport_height == 0
	assert summary.page_info.page_width == 0
	assert summary.page_info.page_height == 0
	assert summary.page_info.scroll_x == 0
	assert summary.page_info.scroll_y == 0
	assert summary.page_info.pixels_above == 0
	assert summary.page_info.pixels_below == 0
	assert summary.page_info.pixels_left == 0
	assert summary.page_info.pixels_right == 0
	assert any('Safari screenshot failed' in error for error in summary.browser_errors)
	assert any('Safari page metrics failed' in error for error in summary.browser_errors)
