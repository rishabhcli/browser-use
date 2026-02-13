"""Tests for Safari browser-state fallback behavior."""

import pytest

from browser_use.browser.events import BrowserStateRequestEvent
from browser_use.browser.views import BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import SerializedDOMState
from safari_session import SafariBrowserSession


def _build_page_info() -> PageInfo:
	"""Create a deterministic page-info object for assertions."""
	return PageInfo(
		viewport_width=1280,
		viewport_height=720,
		page_width=1920,
		page_height=2800,
		scroll_x=10,
		scroll_y=200,
		pixels_above=200,
		pixels_below=1880,
		pixels_left=10,
		pixels_right=630,
	)


@pytest.mark.asyncio
async def test_browser_state_uses_cached_dom_on_extraction_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	"""DOM extraction failures should fall back to cached DOM state when available."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_get_url() -> str:
		return 'https://example.com'

	async def fake_get_title() -> str:
		return 'Example'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def failing_rebuild_dom_state() -> SerializedDOMState:
		raise RuntimeError('dom extraction failed')

	async def fake_page_info() -> PageInfo:
		return _build_page_info()

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', failing_rebuild_dom_state)
	monkeypatch.setattr(session, '_compute_page_info', fake_page_info)

	cached_dom_state = SerializedDOMState(_root=None, selector_map={})
	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=cached_dom_state,
		url='https://cached.example',
		title='Cached',
		tabs=[TabInfo(url='https://cached.example', title='Cached', target_id='cached-target', parent_target_id=None)],
		screenshot=None,
		page_info=_build_page_info(),
	)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.dom_state is cached_dom_state
	assert any('Safari DOM extraction failed' in error for error in summary.browser_errors)
	assert 'dom_fallback:cached_or_empty' in session._recent_events


@pytest.mark.asyncio
async def test_browser_state_uses_cached_page_info_on_metrics_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Page-info failures should fall back to cached page_info when available."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_get_url() -> str:
		return 'https://example.com'

	async def fake_get_title() -> str:
		return 'Example'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def failing_page_info() -> PageInfo:
		raise RuntimeError('metrics unavailable')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session, '_compute_page_info', failing_page_info)

	cached_page_info = _build_page_info()
	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={}),
		url='https://cached.example',
		title='Cached',
		tabs=[TabInfo(url='https://cached.example', title='Cached', target_id='cached-target', parent_target_id=None)],
		screenshot=None,
		page_info=cached_page_info,
	)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.page_info is cached_page_info
	assert summary.pixels_above == cached_page_info.pixels_above
	assert summary.pixels_below == cached_page_info.pixels_below
	assert any('Safari page metrics failed' in error for error in summary.browser_errors)


@pytest.mark.asyncio
async def test_browser_state_defaults_page_info_when_metrics_fail_without_cache(monkeypatch: pytest.MonkeyPatch) -> None:
	"""When metrics fail and no cached page_info exists, Safari should return zeroed page info."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_get_url() -> str:
		return 'https://example.com'

	async def fake_get_title() -> str:
		return 'Example'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def failing_page_info() -> PageInfo:
		raise RuntimeError('metrics unavailable')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session, '_compute_page_info', failing_page_info)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.page_info is not None
	assert summary.page_info.viewport_width == 0
	assert summary.page_info.viewport_height == 0
	assert summary.page_info.page_width == 0
	assert summary.page_info.page_height == 0
	assert summary.pixels_above == 0
	assert summary.pixels_below == 0
	assert any('Safari page metrics failed' in error for error in summary.browser_errors)


@pytest.mark.asyncio
async def test_browser_state_screenshot_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Screenshot errors should not fail state collection and should surface in browser_errors."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_get_url() -> str:
		return 'https://example.com'

	async def fake_get_title() -> str:
		return 'Example'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def fake_screenshot() -> str:
		raise RuntimeError('screenshot unavailable')

	async def fake_page_info() -> PageInfo:
		return _build_page_info()

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session.driver, 'screenshot', fake_screenshot)
	monkeypatch.setattr(session, '_compute_page_info', fake_page_info)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=True))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.screenshot is None
	assert any('Safari screenshot failed' in error for error in summary.browser_errors)


@pytest.mark.asyncio
async def test_browser_state_uses_cached_screenshot_on_capture_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Screenshot failures should reuse cached screenshot when one is available."""
	session = SafariBrowserSession()
	cached_screenshot = 'cached-base64-screenshot'

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_get_url() -> str:
		return 'https://example.com'

	async def fake_get_title() -> str:
		return 'Example'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def fake_screenshot() -> str:
		raise RuntimeError('screenshot unavailable')

	async def fake_page_info() -> PageInfo:
		return _build_page_info()

	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={}),
		url='https://cached.example',
		title='Cached',
		tabs=[TabInfo(url='https://cached.example', title='Cached', target_id='cached-target', parent_target_id=None)],
		screenshot=cached_screenshot,
		page_info=_build_page_info(),
	)

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session.driver, 'screenshot', fake_screenshot)
	monkeypatch.setattr(session, '_compute_page_info', fake_page_info)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=True))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.screenshot == cached_screenshot
	assert any('Safari screenshot failed' in error for error in summary.browser_errors)
	assert 'screenshot_fallback:cached' in session._recent_events


@pytest.mark.asyncio
async def test_browser_state_uses_cached_url_title_tabs_when_metadata_reads_fail(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""URL/title/tab failures should use cached metadata instead of failing state collection."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def failing_get_url() -> str:
		raise RuntimeError('url unavailable')

	async def failing_get_title() -> str:
		raise RuntimeError('title unavailable')

	async def failing_refresh_tabs() -> list[TabInfo]:
		raise RuntimeError('tabs unavailable')

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def fake_page_info() -> PageInfo:
		return _build_page_info()

	cached_tabs = [TabInfo(url='https://cached.example', title='Cached tab', target_id='cached-target', parent_target_id=None)]
	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={}),
		url='https://cached.example',
		title='Cached title',
		tabs=cached_tabs,
		screenshot=None,
		page_info=_build_page_info(),
	)

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', failing_get_url)
	monkeypatch.setattr(session.driver, 'get_title', failing_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', failing_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session, '_compute_page_info', fake_page_info)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.url == 'https://cached.example'
	assert summary.title == 'Cached title'
	assert summary.tabs == cached_tabs
	assert any('Safari URL read failed' in error for error in summary.browser_errors)
	assert any('Safari title read failed' in error for error in summary.browser_errors)
	assert any('Safari tab refresh failed' in error for error in summary.browser_errors)


@pytest.mark.asyncio
async def test_browser_state_creates_fallback_tab_when_tab_refresh_fails_without_cache(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""Without cached tabs, tab-refresh failures should still return a synthetic active tab."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_get_url() -> str:
		return 'https://live.example'

	async def fake_get_title() -> str:
		return 'Live title'

	async def failing_refresh_tabs() -> list[TabInfo]:
		raise RuntimeError('tabs unavailable')

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def fake_page_info() -> PageInfo:
		return _build_page_info()

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', failing_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session, '_compute_page_info', fake_page_info)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert len(summary.tabs) == 1
	assert summary.tabs[0].url == 'https://live.example'
	assert summary.tabs[0].title == 'Live title'
	assert summary.tabs[0].target_id == 'safari-target'
	assert any('Safari tab refresh failed' in error for error in summary.browser_errors)


@pytest.mark.asyncio
async def test_browser_state_download_tracking_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Download tracking failures should not fail browser-state requests."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def failing_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		raise RuntimeError('download tracker unavailable')

	async def fake_get_url() -> str:
		return 'https://example.com'

	async def fake_get_title() -> str:
		return 'Example'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def fake_rebuild_dom_state() -> SerializedDOMState:
		return SerializedDOMState(_root=None, selector_map={})

	async def fake_page_info() -> PageInfo:
		return _build_page_info()

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_download_tracking', failing_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'get_url', fake_get_url)
	monkeypatch.setattr(session.driver, 'get_title', fake_get_title)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_dom_state)
	monkeypatch.setattr(session, '_compute_page_info', fake_page_info)

	try:
		summary = await session.on_BrowserStateRequestEvent(BrowserStateRequestEvent(include_dom=True, include_screenshot=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert summary.url == 'https://example.com'
	assert summary.title == 'Example'
	assert len(summary.tabs) == 1
	assert any('Safari download tracking failed' in error for error in summary.browser_errors)
