"""Tests for Safari dialog/popup handling behavior."""

import pytest

from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.views import TabInfo
from safari_session import SafariBrowserSession


@pytest.mark.asyncio
async def test_dismiss_dialog_records_recent_event_when_handled(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Successful dialog auto-accept should be recorded in session memory."""
	session = SafariBrowserSession()

	async def fake_handle_dialog(accept: bool = True) -> bool:
		del accept
		return True

	monkeypatch.setattr(session.driver, 'handle_dialog', fake_handle_dialog)

	try:
		await session._dismiss_dialog_if_any()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert session._closed_popup_messages
	assert session._closed_popup_messages[-1] == 'Closed JavaScript dialog automatically.'
	assert session._recent_events
	assert session._recent_events[-1] == 'dialog:auto-accepted'


@pytest.mark.asyncio
async def test_dismiss_dialogs_after_navigation_polls_for_delayed_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Delayed onbeforeunload-style dialogs should be caught by polling after navigation."""
	session = SafariBrowserSession()
	results = iter([False, True, False])
	sleep_calls: list[float] = []

	async def fake_dismiss_dialog_if_any() -> bool:
		return next(results)

	async def fake_sleep(seconds: float) -> None:
		sleep_calls.append(seconds)

	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)
	monkeypatch.setattr('safari_session.session.asyncio.sleep', fake_sleep)

	try:
		handled_any = await session._dismiss_dialogs_after_navigation(max_wait_seconds=1.0, poll_interval_seconds=0.0)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert handled_any is True
	assert len(sleep_calls) == 2


@pytest.mark.asyncio
async def test_dismiss_dialogs_after_navigation_returns_false_when_no_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Polling should stop quickly when no dialog is present."""
	session = SafariBrowserSession()
	calls = 0
	sleep_calls: list[float] = []

	async def fake_dismiss_dialog_if_any() -> bool:
		nonlocal calls
		calls += 1
		return False

	async def fake_sleep(seconds: float) -> None:
		sleep_calls.append(seconds)

	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)
	monkeypatch.setattr('safari_session.session.asyncio.sleep', fake_sleep)

	try:
		handled_any = await session._dismiss_dialogs_after_navigation(max_wait_seconds=1.0, poll_interval_seconds=0.0)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert handled_any is False
	assert calls == 2
	assert len(sleep_calls) == 1


@pytest.mark.asyncio
async def test_navigate_event_invokes_dialog_dismissal(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Navigation handler should invoke dialog dismissal in its action flow."""
	session = SafariBrowserSession()
	dismiss_called = False

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del operation_name, retries
		return await operation()

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_navigate(url: str) -> str:
		return url

	async def fake_wait_for_ready_state(timeout_seconds: float = 12.0) -> str:
		del timeout_seconds
		return 'complete'

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	async def fake_dismiss_dialogs_after_navigation(max_wait_seconds: float = 0.8, poll_interval_seconds: float = 0.15) -> bool:
		del max_wait_seconds, poll_interval_seconds
		nonlocal dismiss_called
		dismiss_called = True
		return True

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session.driver, 'navigate', fake_navigate)
	monkeypatch.setattr(session.driver, 'wait_for_ready_state', fake_wait_for_ready_state)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_dismiss_dialogs_after_navigation', fake_dismiss_dialogs_after_navigation)

	try:
		await session.on_NavigateToUrlEvent(NavigateToUrlEvent(url='https://example.com', new_tab=False))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert dismiss_called is True
