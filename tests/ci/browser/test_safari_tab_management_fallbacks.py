"""Tests for Safari tab-management AppleScript fallbacks."""

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.events import CloseTabEvent, SwitchTabEvent
from browser_use.browser.views import TabInfo
from safari_session.driver import SafariTabInfo
from safari_session.session import SafariBrowserSession


def _tab(target_id: str) -> TabInfo:
	return TabInfo(url='https://example.com', title='Example', target_id=target_id, parent_target_id=None)


@pytest.mark.asyncio
async def test_switch_tab_uses_applescript_when_handle_missing(monkeypatch: pytest.MonkeyPatch) -> None:
	"""SwitchTab should fall back to AppleScript when no webdriver handle mapping exists."""
	session = SafariBrowserSession()
	target_id = 'safari-target-1'
	switch_calls: list[int] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		tabs = [_tab(target_id)]
		session._tabs_cache = tabs
		session._target_to_handle = {}
		session._handle_to_target = {}
		return tabs

	async def fake_switch_tab(tab_index: int, timeout_seconds: float = 2.5) -> bool:
		del timeout_seconds
		switch_calls.append(tab_index)
		return True

	async def fake_sleep(seconds: float) -> None:
		del seconds
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(safari_session_module, 'safari_switch_tab', fake_switch_tab)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		active_target = await session.on_SwitchTabEvent(SwitchTabEvent(target_id=target_id))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert active_target == target_id
	assert switch_calls == [0]
	assert any(event.startswith('switch_tab_fallback:') for event in session._recent_events)


@pytest.mark.asyncio
async def test_switch_tab_with_none_target_uses_most_recent_cached_tab(monkeypatch: pytest.MonkeyPatch) -> None:
	"""SwitchTab(target_id=None) should focus the most recently opened cached tab."""
	session = SafariBrowserSession()
	switch_calls: list[str] = []
	tabs = [_tab('safari-target-a'), _tab('safari-target-b')]
	handle_map = {'safari-target-a': 'handle-a', 'safari-target-b': 'handle-b'}

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		session._tabs_cache = tabs
		session._target_to_handle = dict(handle_map)
		session._handle_to_target = {v: k for k, v in handle_map.items()}
		return tabs

	async def fake_switch_to_handle(driver_handle: str) -> SafariTabInfo:
		switch_calls.append(driver_handle)
		return SafariTabInfo(index=1, handle=driver_handle, url='https://example.com', title='Example')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session.driver, 'switch_to_handle', fake_switch_to_handle)

	try:
		active_target = await session.on_SwitchTabEvent(SwitchTabEvent(target_id=None))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert active_target == 'safari-target-b'
	assert switch_calls == ['handle-b']
	assert any(event.startswith('switch_tab:') for event in session._recent_events)


@pytest.mark.asyncio
async def test_switch_tab_with_none_target_opens_new_tab_when_cache_empty(monkeypatch: pytest.MonkeyPatch) -> None:
	"""SwitchTab(target_id=None) should create/focus a new tab when cache is empty."""
	session = SafariBrowserSession()
	refresh_count = 0

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		nonlocal refresh_count
		refresh_count += 1
		if refresh_count == 1:
			session._tabs_cache = []
			session._target_to_handle = {}
			session._handle_to_target = {}
			return []
		tabs = [_tab('safari-target-new')]
		session._tabs_cache = tabs
		session._target_to_handle = {'safari-target-new': 'handle-new'}
		session._handle_to_target = {'handle-new': 'safari-target-new'}
		return tabs

	async def fake_new_tab(url: str = 'about:blank') -> SafariTabInfo:
		assert url == 'about:blank'
		return SafariTabInfo(index=0, handle='handle-new', url='about:blank', title='')

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session.driver, 'new_tab', fake_new_tab)

	try:
		active_target = await session.on_SwitchTabEvent(SwitchTabEvent(target_id=None))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert active_target == 'safari-target-new'
	assert refresh_count == 2


@pytest.mark.asyncio
async def test_switch_tab_with_none_target_falls_back_to_applescript_open_tab(monkeypatch: pytest.MonkeyPatch) -> None:
	"""SwitchTab(target_id=None) should use AppleScript open-tab fallback when webdriver tab creation fails."""
	session = SafariBrowserSession()
	refresh_count = 0
	open_tab_calls: list[str] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		nonlocal refresh_count
		refresh_count += 1
		if refresh_count == 1:
			session._tabs_cache = []
			session._target_to_handle = {}
			session._handle_to_target = {}
			return []
		tabs = [_tab('safari-target-fallback')]
		session._tabs_cache = tabs
		return tabs

	async def fake_new_tab(url: str = 'about:blank') -> SafariTabInfo:
		del url
		raise RuntimeError('webdriver new tab failed')

	async def fake_open_tab(url: str, timeout_seconds: float = 2.5) -> None:
		del timeout_seconds
		open_tab_calls.append(url)

	async def fake_sleep(seconds: float) -> None:
		del seconds
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session.driver, 'new_tab', fake_new_tab)
	monkeypatch.setattr(safari_session_module, 'safari_open_tab', fake_open_tab)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		active_target = await session.on_SwitchTabEvent(SwitchTabEvent(target_id=None))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert active_target == 'safari-target-fallback'
	assert open_tab_calls == ['about:blank']


@pytest.mark.asyncio
async def test_switch_tab_uses_applescript_when_webdriver_switch_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	"""SwitchTab should fall back to AppleScript when webdriver switch_to_handle raises."""
	session = SafariBrowserSession()
	target_id = 'safari-target-2'
	handle = 'handle-1'
	switch_calls: list[int] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		tabs = [_tab(target_id)]
		session._tabs_cache = tabs
		session._target_to_handle = {target_id: handle}
		session._handle_to_target = {handle: target_id}
		return tabs

	async def fake_switch_to_handle(driver_handle: str) -> SafariTabInfo:
		del driver_handle
		raise RuntimeError('switch failed')

	async def fake_switch_tab(tab_index: int, timeout_seconds: float = 2.5) -> bool:
		del timeout_seconds
		switch_calls.append(tab_index)
		return True

	async def fake_sleep(seconds: float) -> None:
		del seconds
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session.driver, 'switch_to_handle', fake_switch_to_handle)
	monkeypatch.setattr(safari_session_module, 'safari_switch_tab', fake_switch_tab)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		active_target = await session.on_SwitchTabEvent(SwitchTabEvent(target_id=target_id))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert active_target == target_id
	assert switch_calls == [0]
	assert any(event.startswith('switch_tab_fallback:') for event in session._recent_events)


@pytest.mark.asyncio
async def test_close_tab_uses_applescript_when_handle_missing(monkeypatch: pytest.MonkeyPatch) -> None:
	"""CloseTab should fall back to AppleScript when target-handle mapping is missing."""
	session = SafariBrowserSession()
	target_id = 'safari-target-3'
	close_calls: list[int] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		tabs = [_tab(target_id)]
		session._tabs_cache = tabs
		session._target_to_handle = {}
		session._handle_to_target = {}
		return tabs

	async def fake_close_tab(tab_index: int, timeout_seconds: float = 2.5) -> bool:
		del timeout_seconds
		close_calls.append(tab_index)
		return True

	async def fake_sleep(seconds: float) -> None:
		del seconds
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(safari_session_module, 'safari_close_tab', fake_close_tab)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		await session.on_CloseTabEvent(CloseTabEvent(target_id=target_id))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert close_calls == [0]
	assert any(event.startswith('close_tab_fallback:') for event in session._recent_events)


@pytest.mark.asyncio
async def test_close_tab_uses_applescript_when_webdriver_close_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	"""CloseTab should fall back to AppleScript when webdriver close_tab raises."""
	session = SafariBrowserSession()
	target_id = 'safari-target-4'
	handle = 'handle-2'
	close_calls: list[int] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		tabs = [_tab(target_id)]
		session._tabs_cache = tabs
		session._target_to_handle = {target_id: handle}
		session._handle_to_target = {handle: target_id}
		return tabs

	async def fake_list_tabs() -> list[SafariTabInfo]:
		return [SafariTabInfo(index=0, handle=handle, url='https://example.com', title='Example')]

	async def fake_driver_close_tab(index: int | None = None) -> SafariTabInfo | None:
		del index
		raise RuntimeError('close failed')

	async def fake_close_tab(tab_index: int, timeout_seconds: float = 2.5) -> bool:
		del timeout_seconds
		close_calls.append(tab_index)
		return True

	async def fake_sleep(seconds: float) -> None:
		del seconds
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session.driver, 'list_tabs', fake_list_tabs)
	monkeypatch.setattr(session.driver, 'close_tab', fake_driver_close_tab)
	monkeypatch.setattr(safari_session_module, 'safari_close_tab', fake_close_tab)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		await session.on_CloseTabEvent(CloseTabEvent(target_id=target_id))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert close_calls == [0]
	assert any(event.startswith('close_tab_fallback:') for event in session._recent_events)
