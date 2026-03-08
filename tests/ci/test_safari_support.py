"""Tests for Safari backend helpers."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from browser_use.browser.backends.base import BackendCapabilityReport
from browser_use.browser.backends.safari_backend import SafariRealProfileBackend, _run_applescript_sync, _run_jxa_sync
from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.safari.capabilities import SafariCapabilityReport, probe_safari_environment
from browser_use.browser.safari.profiles import SafariProfileStore
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserError


def test_probe_safari_environment_uses_local_backend_probe(tmp_path: Path):
	"""Legacy capability probe should reflect the current built-in Safari backend, not a host socket."""
	socket_path = tmp_path / 'host.sock'

	with patch(
		'browser_use.browser.safari.capabilities.probe_local_safari_backend',
		return_value=BackendCapabilityReport(
			backend='safari',
			available=True,
			details={
				'safari_version': '26.3.1',
				'macos_version': '26.0',
				'gui_scripting_available': True,
				'screen_capture_available': True,
			},
		),
	):
		report = probe_safari_environment(socket_path)

	assert isinstance(report, SafariCapabilityReport)
	assert report.supported is True
	assert report.host_socket_path == socket_path
	assert all('host socket' not in issue.lower() for issue in report.issues)


def test_safari_profile_store_round_trip(tmp_path: Path):
	"""Safari profile bindings should persist to disk."""
	store_path = tmp_path / 'profiles.json'
	store = SafariProfileStore(store_path)

	store.bind('Personal', 'profile-personal', last_seen_target_id='tab-1234')
	store.bind('Work', 'profile-work')

	data = json.loads(store_path.read_text())
	assert len(data['bindings']) == 2
	assert store.get_identifier('Personal') == 'profile-personal'
	assert store.get_label('profile-work') == 'Work'


def test_browser_session_reports_safari_capabilities_without_start():
	"""Safari BrowserSession should expose local backend capabilities before startup."""
	report = BackendCapabilityReport(
		backend='safari',
		available=True,
		details={
			'safari_version': '26.3.1',
			'macos_version': '26.0',
			'gui_scripting_available': True,
			'screen_capture_available': True,
			'profile': 'Personal',
		},
	)
	session = BrowserSession(automation_backend='safari', safari_profile='Personal', headless=False)

	with patch('browser_use.browser.backends.safari_backend.probe_local_safari_backend', return_value=report):
		capabilities = session.get_backend_capabilities()

	assert capabilities.backend_name == 'safari'
	assert capabilities.browser_version == '26.3.1'
	assert capabilities.supported is True
	assert capabilities.supports_real_profile is True
	assert session.backend_capabilities == report


def test_safari_guards_preserve_browser_session_docstrings():
	"""Safari-only guard clauses should not erase method docstrings."""
	get_cdp_doc = BrowserSession.get_or_create_cdp_session.__doc__
	client_for_node_doc = BrowserSession.cdp_client_for_node.__doc__

	assert get_cdp_doc is not None
	assert get_cdp_doc.lstrip().startswith('Get CDP session for a target')
	assert client_for_node_doc is not None
	assert client_for_node_doc.lstrip().startswith('Get CDP client for a specific DOM node')


def test_run_jxa_sync_wraps_timeout_as_browser_error():
	"""Timeouts from osascript should be normalized to BrowserError."""
	with patch(
		'browser_use.browser.backends.safari_backend.subprocess.run',
		side_effect=subprocess.TimeoutExpired(cmd=['osascript'], timeout=3, output='partial stdout', stderr='partial stderr'),
	):
		with pytest.raises(BrowserError, match='timed out') as exc_info:
			_run_jxa_sync('return 1;', timeout=3)

	assert exc_info.value.details == {
		'timeout_seconds': 3,
		'stderr': 'partial stderr',
		'stdout': 'partial stdout',
	}


@pytest.mark.asyncio
async def test_safari_take_screenshot_closes_mkstemp_fd(tmp_path: Path):
	"""Temporary screenshot descriptors should be closed immediately."""
	output_path = tmp_path / 'safari-shot.png'
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	object.__setattr__(backend, '_ensure_profile_window', AsyncMock(return_value={'windowId': 123}))

	async def fake_to_thread(*args, **kwargs):
		output_path.write_bytes(b'png-bytes')
		return None

	with (
		patch('browser_use.browser.backends.safari_backend.tempfile.mkstemp', return_value=(99, str(output_path))),
		patch('browser_use.browser.backends.safari_backend.os.close') as close_fd,
		patch('browser_use.browser.backends.safari_backend.asyncio.to_thread', new=AsyncMock(side_effect=fake_to_thread)),
	):
		data = await backend.take_screenshot()

	assert data == b'png-bytes'
	close_fd.assert_called_once_with(99)
	assert not output_path.exists()


@pytest.mark.asyncio
async def test_safari_hover_element_uses_dom_mouse_events():
	"""Safari hover helper should synthesize hover events through page JavaScript."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	object.__setattr__(backend, '_ensure_profile_window', AsyncMock(return_value={'windowId': 123}))
	evaluate_javascript = AsyncMock(return_value={'ok': True})
	object.__setattr__(backend, 'evaluate_javascript', evaluate_javascript)
	node = cast(Any, SimpleNamespace(backend_node_id=17))

	result = await backend.hover_element(node)

	assert result == {'ok': True}
	evaluate_javascript.assert_awaited_once()
	await_args = evaluate_javascript.await_args
	assert await_args is not None
	script = await_args.args[0]
	assert 'pointerover' in script
	assert 'mousemove' in script
	assert 'data-browser-use-safari-id="17"' in script


@pytest.mark.asyncio
async def test_safari_click_element_uses_mouse_sequence_before_native_click():
	"""Safari click helper should emit down/up events before triggering the native click."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	object.__setattr__(backend, '_ensure_profile_window', AsyncMock(return_value={'windowId': 123}))
	refresh_focus_target = AsyncMock(return_value=None)
	evaluate_javascript = AsyncMock(return_value={'ok': True})
	object.__setattr__(backend, '_refresh_focus_target', refresh_focus_target)
	object.__setattr__(backend, 'evaluate_javascript', evaluate_javascript)
	node = cast(Any, SimpleNamespace(backend_node_id=19))

	result = await backend.click_element(node)

	assert result == {'ok': True}
	evaluate_javascript.assert_awaited_once()
	await_args = evaluate_javascript.await_args
	assert await_args is not None
	script = await_args.args[0]
	assert 'pointerdown' in script
	assert 'mousedown' in script
	assert 'mouseup' in script
	assert 'target.click()' in script
	assert 'closest(interactiveSelector)' in script
	refresh_focus_target.assert_awaited_once()


@pytest.mark.asyncio
async def test_safari_double_click_element_uses_dom_double_click_events():
	"""Safari double-click helper should synthesize a dblclick sequence through page JavaScript."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	object.__setattr__(backend, '_ensure_profile_window', AsyncMock(return_value={'windowId': 123}))
	refresh_focus_target = AsyncMock(return_value=None)
	evaluate_javascript = AsyncMock(return_value={'ok': True})
	object.__setattr__(backend, '_refresh_focus_target', refresh_focus_target)
	object.__setattr__(backend, 'evaluate_javascript', evaluate_javascript)
	node = cast(Any, SimpleNamespace(backend_node_id=23))

	result = await backend.double_click_element(node)

	assert result == {'ok': True}
	evaluate_javascript.assert_awaited_once()
	await_args = evaluate_javascript.await_args
	assert await_args is not None
	script = await_args.args[0]
	assert 'dblclick' in script
	assert 'mousedown' in script
	assert 'data-browser-use-safari-id="23"' in script
	refresh_focus_target.assert_awaited_once()


@pytest.mark.asyncio
async def test_safari_focus_existing_profile_window_uses_boolean_result_script():
	"""Existing Safari profile windows should report focus success through the final JSON payload."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)

	async def fake_run_jxa_json(script: str):
		assert 'let focused = false;' in script
		assert 'JSON.stringify({ focused });' in script
		return {'focused': True}

	object.__setattr__(backend, '_run_jxa_json', AsyncMock(side_effect=fake_run_jxa_json))

	result = await backend._focus_existing_profile_window('Personal')

	assert result is True


@pytest.mark.asyncio
async def test_safari_open_profile_window_uses_applescript_menu_click():
	"""Safari should invoke profile menu items through AppleScript, not JXA menu-item click methods."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	object.__setattr__(backend, '_probe_gui_scripting', AsyncMock(return_value=True))
	run_jxa_json = AsyncMock(return_value={'exists': True})
	object.__setattr__(backend, '_run_jxa_json', run_jxa_json)

	with patch(
		'browser_use.browser.backends.safari_backend.asyncio.to_thread',
		new=AsyncMock(return_value=''),
	) as to_thread_mock:
		await backend._open_profile_window('Personal')

	assert run_jxa_json.await_count == 1
	await_args = run_jxa_json.await_args
	assert await_args is not None
	assert '.click()' not in await_args.args[0]
	to_thread_mock.assert_awaited_once()
	to_thread_args = to_thread_mock.await_args
	assert to_thread_args is not None
	assert to_thread_args.args[0] is _run_applescript_sync
	assert 'click menu item "New Personal Window"' in to_thread_args.args[1]


@pytest.mark.asyncio
async def test_safari_watchdog_dispatches_navigation_without_cdp():
	"""Safari watchdog navigation should not be skipped just because there is no CDP socket."""
	session = BrowserSession(automation_backend='safari', headless=False)
	await session.attach_all_watchdogs()

	assert isinstance(session._backend, SafariRealProfileBackend)
	navigate_to = AsyncMock(return_value=None)
	get_current_page_url = AsyncMock(return_value='https://www.amazon.com/')
	object.__setattr__(session._backend, 'navigate_to', navigate_to)
	object.__setattr__(session._backend, 'get_current_page_url', get_current_page_url)
	session.agent_focus_target_id = 'safari:1:1'

	event = session.event_bus.dispatch(NavigateToUrlEvent(url='https://www.amazon.com/'))
	await event
	await event.event_result(raise_if_any=True, raise_if_none=False)

	navigate_to.assert_awaited_once_with('https://www.amazon.com/', new_tab=False)
	get_current_page_url.assert_awaited_once()
