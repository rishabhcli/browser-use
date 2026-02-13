"""Tests for Safari session lifecycle and initialization/reset behavior."""

from __future__ import annotations

from typing import Any

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.views import BrowserStateSummary
from safari_session import SafariBrowserSession
from safari_session.driver import SafariDriverConfig


@pytest.mark.asyncio
async def test_from_config_binds_driver_config() -> None:
	config = SafariDriverConfig(executable_path='/custom/safaridriver', command_timeout=12.0)
	session = SafariBrowserSession.from_config(config)
	try:
		assert session.driver.config.executable_path == '/custom/safaridriver'
		assert session.driver.config.command_timeout == 12.0
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_start_initializes_shims_and_registers_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	start_calls = 0
	load_calls = 0
	refresh_tabs_calls = 0

	async def fake_driver_start() -> None:
		nonlocal start_calls
		start_calls += 1

	async def fake_driver_is_alive() -> bool:
		return True

	async def fake_refresh_tabs() -> list[Any]:
		nonlocal refresh_tabs_calls
		refresh_tabs_calls += 1
		return []

	async def fake_refresh_applescript_download_dir() -> None:
		return None

	async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
		return {'/tmp/a.txt': (1, 1.0)}

	async def fake_load_storage_state(self: SafariBrowserSession) -> None:
		nonlocal load_calls
		del self
		load_calls += 1

	monkeypatch.setattr(session.driver, 'start', fake_driver_start)
	monkeypatch.setattr(session.driver, 'is_alive', fake_driver_is_alive)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_refresh_applescript_download_dir', fake_refresh_applescript_download_dir)
	monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
	monkeypatch.setattr(SafariBrowserSession, 'load_storage_state', fake_load_storage_state)

	try:
		await session.start()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert start_calls == 1
	assert refresh_tabs_calls == 1
	assert load_calls == 1
	assert session._started is True
	assert session.cdp_url == 'safari://webdriver'
	assert session._cdp_shim is not None
	assert session._dom_watchdog is not None
	assert session._download_snapshot == {'/tmp/a.txt': (1, 1.0)}

	for event_name in [
		'NavigateToUrlEvent',
		'ClickElementEvent',
		'TypeTextEvent',
		'ScrollEvent',
		'ScreenshotEvent',
		'BrowserStateRequestEvent',
		'SwitchTabEvent',
		'CloseTabEvent',
		'SendKeysEvent',
	]:
		assert session.event_bus.handlers.get(event_name), f'missing handler for {event_name}'


@pytest.mark.asyncio
async def test_start_activates_requested_safari_profile_window(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession(safari_profile_name='School')
	calls: list[str] = []

	async def fake_open_profile_window(profile_name: str, timeout_seconds: float = 6.0) -> str:
		del timeout_seconds
		calls.append(f'profile:{profile_name}')
		return 'New School Window'

	async def fake_driver_start() -> None:
		calls.append('driver:start')

	async def fake_driver_is_alive() -> bool:
		return True

	async def fake_refresh_tabs() -> list[Any]:
		return []

	async def fake_refresh_applescript_download_dir() -> None:
		return None

	async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
		return {}

	async def fake_load_storage_state(self: SafariBrowserSession) -> None:
		del self
		return None

	monkeypatch.setattr(safari_session_module, 'safari_open_profile_window', fake_open_profile_window)
	monkeypatch.setattr(session.driver, 'start', fake_driver_start)
	monkeypatch.setattr(session.driver, 'is_alive', fake_driver_is_alive)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_refresh_applescript_download_dir', fake_refresh_applescript_download_dir)
	monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
	monkeypatch.setattr(SafariBrowserSession, 'load_storage_state', fake_load_storage_state)

	try:
		await session.start()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert calls == ['profile:School', 'driver:start']
	assert session._started is True
	assert any(evt.startswith('profile:New School Window') for evt in session._recent_events)


@pytest.mark.asyncio
async def test_start_continues_when_requested_profile_window_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession(safari_profile_name='School')
	driver_start_calls = 0

	async def fake_open_profile_window(profile_name: str, timeout_seconds: float = 6.0) -> str:
		del profile_name, timeout_seconds
		raise RuntimeError('no matching profile')

	async def fake_driver_start() -> None:
		nonlocal driver_start_calls
		driver_start_calls += 1

	async def fake_driver_is_alive() -> bool:
		return True

	async def fake_refresh_tabs() -> list[Any]:
		return []

	async def fake_refresh_applescript_download_dir() -> None:
		return None

	async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
		return {}

	async def fake_load_storage_state(self: SafariBrowserSession) -> None:
		del self
		return None

	monkeypatch.setattr(safari_session_module, 'safari_open_profile_window', fake_open_profile_window)
	monkeypatch.setattr(session.driver, 'start', fake_driver_start)
	monkeypatch.setattr(session.driver, 'is_alive', fake_driver_is_alive)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_refresh_applescript_download_dir', fake_refresh_applescript_download_dir)
	monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
	monkeypatch.setattr(SafariBrowserSession, 'load_storage_state', fake_load_storage_state)

	try:
		await session.start()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert driver_start_calls == 1
	assert session._started is True
	assert any(evt.startswith('profile:failed:School') for evt in session._recent_events)


@pytest.mark.asyncio
async def test_start_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	start_calls = 0

	async def fake_driver_start() -> None:
		nonlocal start_calls
		start_calls += 1

	async def fake_driver_is_alive() -> bool:
		return True

	async def fake_refresh_tabs() -> list[Any]:
		return []

	async def fake_refresh_applescript_download_dir() -> None:
		return None

	async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
		return {}

	async def fake_load_storage_state(self: SafariBrowserSession) -> None:
		del self
		return None

	monkeypatch.setattr(session.driver, 'start', fake_driver_start)
	monkeypatch.setattr(session.driver, 'is_alive', fake_driver_is_alive)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_refresh_applescript_download_dir', fake_refresh_applescript_download_dir)
	monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
	monkeypatch.setattr(SafariBrowserSession, 'load_storage_state', fake_load_storage_state)

	try:
		await session.start()
		await session.start()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert start_calls == 1


@pytest.mark.asyncio
async def test_start_retries_driver_when_profile_startup_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession(safari_profile_name='School')
	start_calls = 0
	close_calls = 0
	is_alive_checks = 0

	async def fake_open_profile_window(profile_name: str, timeout_seconds: float = 6.0) -> str:
		del profile_name, timeout_seconds
		return 'New School Window'

	async def fake_driver_start() -> None:
		nonlocal start_calls
		start_calls += 1

	async def fake_driver_close() -> None:
		nonlocal close_calls
		close_calls += 1

	async def fake_driver_is_alive() -> bool:
		nonlocal is_alive_checks
		is_alive_checks += 1
		# First check fails (triggers retry), second check succeeds.
		return is_alive_checks >= 2

	async def fake_refresh_tabs() -> list[Any]:
		return []

	async def fake_refresh_applescript_download_dir() -> None:
		return None

	async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
		return {}

	async def fake_load_storage_state(self: SafariBrowserSession) -> None:
		del self
		return None

	monkeypatch.setattr(safari_session_module, 'safari_open_profile_window', fake_open_profile_window)
	monkeypatch.setattr(session.driver, 'start', fake_driver_start)
	monkeypatch.setattr(session.driver, 'close', fake_driver_close)
	monkeypatch.setattr(session.driver, 'is_alive', fake_driver_is_alive)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_refresh_applescript_download_dir', fake_refresh_applescript_download_dir)
	monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
	monkeypatch.setattr(SafariBrowserSession, 'load_storage_state', fake_load_storage_state)

	try:
		await session.start()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert start_calls == 2
	assert close_calls == 1
	assert session._started is True


@pytest.mark.asyncio
async def test_start_recovers_when_profile_startup_raises(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession(safari_profile_name='School')
	start_calls = 0

	async def fake_open_profile_window(profile_name: str, timeout_seconds: float = 6.0) -> str:
		del profile_name, timeout_seconds
		return 'New School Window'

	async def fake_driver_start() -> None:
		nonlocal start_calls
		start_calls += 1
		if start_calls == 1:
			raise RuntimeError('startup hang')

	async def fake_driver_is_alive() -> bool:
		return True

	async def fake_refresh_tabs() -> list[Any]:
		return []

	async def fake_refresh_applescript_download_dir() -> None:
		return None

	async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
		return {}

	async def fake_load_storage_state(self: SafariBrowserSession) -> None:
		del self
		return None

	monkeypatch.setattr(safari_session_module, 'safari_open_profile_window', fake_open_profile_window)
	monkeypatch.setattr(session.driver, 'start', fake_driver_start)
	monkeypatch.setattr(session.driver, 'is_alive', fake_driver_is_alive)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)
	monkeypatch.setattr(session, '_refresh_applescript_download_dir', fake_refresh_applescript_download_dir)
	monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
	monkeypatch.setattr(SafariBrowserSession, 'load_storage_state', fake_load_storage_state)

	try:
		await session.start()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert start_calls == 2
	assert session._started is True


@pytest.mark.asyncio
async def test_stop_saves_state_closes_driver_and_resets_internal_state(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	session._started = True
	session.driver._driver = object()
	session._cached_browser_state_summary = BrowserStateSummary(
		dom_state=type('D', (), {'_root': None, 'selector_map': {}, 'llm_representation': lambda self: ''})(),  # type: ignore[arg-type]
		url='https://example.com',
		title='Example',
		tabs=[],
		screenshot='cached',
	)
	session._cached_selector_map = {1: object()}  # type: ignore[assignment]
	session._ref_by_backend_id = {}
	session._tabs_cache = []
	session._target_to_handle = {'target-1': 'handle-1'}
	session._handle_to_target = {'handle-1': 'target-1'}
	session._download_snapshot = {'/tmp/file': (1, 1.0)}
	session._applescript_download_dir = '/Users/test/Downloads'
	session._applescript_download_dir_checked = True
	session._applescript_tabs_cache = []
	session._applescript_tabs_cached_at = 1.0
	session._cdp_client_root = object()

	save_calls = 0
	close_calls = 0

	async def fake_save_storage_state(self: SafariBrowserSession, path: str | None = None) -> None:
		nonlocal save_calls
		del self, path
		save_calls += 1

	async def fake_driver_close() -> None:
		nonlocal close_calls
		close_calls += 1

	monkeypatch.setattr(SafariBrowserSession, 'save_storage_state', fake_save_storage_state)
	monkeypatch.setattr(session.driver, 'close', fake_driver_close)

	try:
		await session.stop()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert save_calls == 1
	assert close_calls == 1
	assert session._started is False
	assert session._cdp_client_root is None
	assert session._cached_browser_state_summary is None
	assert session._cached_selector_map == {}
	assert session._ref_by_backend_id == {}
	assert session._tabs_cache == []
	assert session._target_to_handle == {}
	assert session._handle_to_target == {}
	assert session._download_snapshot is None
	assert session._applescript_download_dir is None
	assert session._applescript_download_dir_checked is False
	assert session._applescript_tabs_cache == []
	assert session._applescript_tabs_cached_at == 0.0


@pytest.mark.asyncio
async def test_stop_continues_when_save_storage_state_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	session._started = True
	session.driver._driver = object()
	close_calls = 0

	async def fake_save_storage_state(self: SafariBrowserSession, path: str | None = None) -> None:
		del self, path
		raise RuntimeError('save failed')

	async def fake_driver_close() -> None:
		nonlocal close_calls
		close_calls += 1

	monkeypatch.setattr(SafariBrowserSession, 'save_storage_state', fake_save_storage_state)
	monkeypatch.setattr(session.driver, 'close', fake_driver_close)

	try:
		await session.stop()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert close_calls == 1
	assert session._started is False


@pytest.mark.asyncio
async def test_close_and_kill_delegate_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	stop_calls = 0

	async def fake_stop(self: SafariBrowserSession) -> None:
		nonlocal stop_calls
		del self
		stop_calls += 1

	monkeypatch.setattr(SafariBrowserSession, 'stop', fake_stop)
	try:
		await session.close()
		await session.kill()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert stop_calls == 2
