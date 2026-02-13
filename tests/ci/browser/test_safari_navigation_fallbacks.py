"""Tests for Safari navigation paths and new-tab fallbacks."""

from __future__ import annotations

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.views import TabInfo
from safari_session.session import SafariBrowserSession


@pytest.mark.asyncio
async def test_navigate_new_tab_falls_back_to_applescript_on_webdriver_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	"""NavigateToUrl(new_tab=True) should use AppleScript fallback when webdriver new_tab fails."""
	session = SafariBrowserSession()
	open_tab_calls: list[str] = []
	download_refresh_calls: list[bool] = []
	refresh_tabs_calls = 0
	dialog_poll_calls = 0

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		assert operation_name == 'NavigateToUrlEvent'
		del retries
		return await operation()

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del timeout_seconds
		download_refresh_calls.append(wait_for_new)
		return []

	async def fake_new_tab(url: str = 'about:blank') -> None:
		del url
		raise RuntimeError('webdriver failed to open tab')

	async def fake_open_tab(url: str, timeout_seconds: float = 2.5) -> None:
		del timeout_seconds
		open_tab_calls.append(url)

	async def fake_sleep(seconds: float) -> None:
		del seconds
		return None

	async def fake_wait_for_ready_state(timeout_seconds: float = 12.0) -> str:
		assert timeout_seconds == 12.0
		return 'complete'

	async def fake_dismiss_dialogs_after_navigation(
		max_wait_seconds: float = 0.8, poll_interval_seconds: float = 0.15
	) -> bool:
		del max_wait_seconds, poll_interval_seconds
		nonlocal dialog_poll_calls
		dialog_poll_calls += 1
		return False

	async def fake_refresh_tabs() -> list[TabInfo]:
		nonlocal refresh_tabs_calls
		refresh_tabs_calls += 1
		tabs = [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]
		session._tabs_cache = tabs
		return tabs

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'new_tab', fake_new_tab)
	monkeypatch.setattr(session.driver, 'wait_for_ready_state', fake_wait_for_ready_state)
	monkeypatch.setattr(session, '_dismiss_dialogs_after_navigation', fake_dismiss_dialogs_after_navigation)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(safari_session_module, 'safari_open_tab', fake_open_tab)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		await session.on_NavigateToUrlEvent(NavigateToUrlEvent(url='https://example.com/new', new_tab=True))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert open_tab_calls == ['https://example.com/new']
	assert download_refresh_calls == [False, True]
	assert refresh_tabs_calls == 1
	assert dialog_poll_calls == 1
	assert 'navigate:https://example.com/new' in session._recent_events
