"""Live Safari/Chromium backend parity contracts."""

from __future__ import annotations

import asyncio
import json
import platform
import socketserver
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from pytest_httpserver import HTTPServer

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.backends.base import BrowserBackendCapabilities
from browser_use.browser.backends.safari_backend import SafariRealProfileBackend, probe_local_safari_backend
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.service import Tools

# Prevent pytest-httpserver shutdown hangs in local runs.
socketserver.ThreadingMixIn.block_on_close = False
socketserver.ThreadingMixIn.daemon_threads = True


@dataclass(slots=True)
class ContractHarness:
	backend: str
	session: BrowserSession
	capabilities: BrowserBackendCapabilities


@pytest.fixture(scope='session')
def parity_http_server():
	server = HTTPServer()
	server.start()

	server.expect_request('/parity').respond_with_data(
		"""
		<!DOCTYPE html>
		<html>
		<head>
			<title>Parity Home</title>
			<style>
				body { font-family: sans-serif; }
				.spacer { height: 1400px; }
			</style>
			<script>
				window.eventLog = [];
				function logEvent(name) {
					window.eventLog.push(name);
					document.getElementById('event-log').textContent = window.eventLog.join(',');
				}
			</script>
		</head>
		<body>
			<h1 id="title">Parity Home</h1>
			<button id="hover-target" onmouseover="logEvent('hover')">Hover</button>
			<button
				id="click-target"
				onpointerdown="logEvent('pointerdown')"
				onmousedown="logEvent('mousedown')"
				onclick="logEvent('click'); document.getElementById('click-status').textContent = 'clicked'"
			>
				Click
			</button>
			<button id="double-target" ondblclick="logEvent('dblclick')">Double Click</button>
			<button
				id="right-target"
				onmousedown="if (event.button === 2) logEvent('rightclick')"
				oncontextmenu="event.preventDefault(); logEvent('rightclick')"
			>
				Right Click
			</button>
			<input
				id="text-input"
				value="seed"
				oninput="document.getElementById('value-mirror').textContent = this.value"
			/>
			<div id="value-mirror">seed</div>
			<div id="text-block" data-live="stale">Initial text block</div>
			<select
				id="dropdown"
				required
				onchange="
					document.getElementById('selected-value').textContent = this.value;
					const continueButton = document.getElementById('continue-target');
					continueButton.disabled = !this.value;
					continueButton.setAttribute('aria-disabled', String(!this.value));
				"
			>
				<option value="">Please select</option>
				<option value="alpha">Alpha</option>
				<option value="beta">Beta</option>
			</select>
			<div id="selected-value"></div>
			<button
				id="continue-target"
				disabled
				aria-disabled="true"
				onclick="logEvent('continue'); document.getElementById('continue-status').textContent = 'continued'"
			>
				Continue
			</button>
			<div id="continue-status"></div>
			<div id="click-status"></div>
			<label id="upload-label" for="file-input">Upload file</label>
			<input
				id="file-input"
				type="file"
				onchange="document.getElementById('file-name').textContent = this.files.length ? this.files[0].name : ''"
			/>
			<div id="file-name"></div>
			<div class="spacer"></div>
			<div id="scroll-target">Needle text target</div>
			<div id="event-log"></div>
		</body>
		</html>
		""",
		content_type='text/html',
	)
	server.expect_request('/page2').respond_with_data(
		'<html><head><title>Parity Page 2</title></head><body><h1>Parity Page 2</h1></body></html>',
		content_type='text/html',
	)
	server.expect_request('/favicon.ico').respond_with_data('', status=204, content_type='image/x-icon')

	yield server
	server.stop()


@pytest.fixture(scope='session')
def parity_base_url(parity_http_server: HTTPServer) -> str:
	return f'http://{parity_http_server.host}:{parity_http_server.port}'


@pytest.fixture(scope='session')
def parity_run_token() -> str:
	return uuid.uuid4().hex


def _parity_url(base_url: str, path: str, run_token: str) -> str:
	query = urlencode({'browser_use_contract': run_token})
	return f'{base_url}{path}?{query}'


@pytest.fixture(scope='session')
def parity_home_url(parity_base_url: str, parity_run_token: str) -> str:
	return _parity_url(parity_base_url, '/parity', parity_run_token)


@pytest.fixture(scope='session')
def parity_page2_url(parity_base_url: str, parity_run_token: str) -> str:
	return _parity_url(parity_base_url, '/page2', parity_run_token)


def _is_safari_available() -> str | None:
	if platform.system() != 'Darwin':
		return 'Safari live parity tests require macOS.'
	report = probe_local_safari_backend()
	if not report.available:
		return report.reason or 'Safari backend is unavailable.'
	return None


def _safari_target_sort_key(target_id: str) -> tuple[int, int]:
	parts = target_id.split(':')
	if len(parts) != 3:
		return (0, 0)
	return int(parts[1]), int(parts[2])


def _is_contract_test_tab(url: str, run_token: str) -> bool:
	try:
		parsed = urlparse(url)
	except ValueError:
		return False
	token = parse_qs(parsed.query).get('browser_use_contract', [None])[0]
	return parsed.hostname in {'localhost', '127.0.0.1'} and token == run_token


async def _open_safari_test_window(session: BrowserSession) -> tuple[int, bool]:
	backend = cast(SafariRealProfileBackend, session._backend)
	assert backend is not None
	get_window_ids = """
		(() => {
			const safari = Application("Safari");
			const ids = Array.from(safari.windows() || []).filter(Boolean).map(w => w.id());
			return JSON.stringify(ids);
		})()
	"""
	before_window_ids = {
		int(w)
		for w in (
			await backend._run_jxa_json(get_window_ids) if isinstance(await backend._run_jxa_json(get_window_ids), list) else []
		)
	}
	applescript = """
	tell application "Safari" to activate
	tell application "System Events"
		keystroke "n" using {command down}
	end tell
	delay 0.4
	"""
	await asyncio.to_thread(
		subprocess.run,
		['osascript'],
		input=applescript,
		capture_output=True,
		text=True,
		check=True,
	)
	candidate_window_id: int | None = None
	created_new_window = False
	for _ in range(6):
		after_window_ids = await backend._run_jxa_json(get_window_ids)
		candidates = [int(w) for w in after_window_ids] if isinstance(after_window_ids, list) else []
		new_window_ids = [window_id for window_id in candidates if window_id not in before_window_ids]
		if new_window_ids:
			candidate_window_id = int(new_window_ids[0])
			created_new_window = True
			break
		await asyncio.sleep(0.2)

	if candidate_window_id is None:
		window = await backend._get_front_window()
		candidate_window_id = int(window['windowId'])

	window = await backend._run_jxa_json(
		f"""
		const safari = Application(\"Safari\");
		const targetWindowId = {json.dumps(candidate_window_id)};
		const windows = Array.from(safari.windows() || []).filter(Boolean);
		const win = windows.find(w => w.id() === targetWindowId);
		if (!win) {{
			JSON.stringify({{}});
		}} else {{
			const tabs = Array.from(win.tabs() || []).filter(Boolean);
			const tab = win.currentTab() || tabs[0] || null;
			JSON.stringify({{
				windowId: win.id(),
				currentTabIndex: tab ? tab.index() : 1,
			}});
		}}
		"""
	)
	if not isinstance(window, dict) or not window:
		window = await backend._get_front_window()
	else:
		window = {
			'windowId': int(window.get('windowId', 0)),
			'currentTabIndex': int(window.get('currentTabIndex', 1)),
		}
	session.agent_focus_target_id = f'safari:{int(window["windowId"])}:{int(window["currentTabIndex"])}'
	if hasattr(backend, '_preferred_window_id'):
		backend._preferred_window_id = int(window['windowId'])
	return int(window['windowId']), created_new_window


async def _close_safari_window(session: BrowserSession, window_id: int) -> None:
	backend = cast(SafariRealProfileBackend, session._backend)
	assert backend is not None
	await backend._run_jxa(
		f"""
		const safari = Application("Safari");
		safari.activate();
		const win = safari.windows().find(w => w.id() === {window_id});
		if (win) {{
			win.close();
		}}
		"""
	)
	await asyncio.sleep(0.2)


async def _settle(harness: ContractHarness) -> None:
	await asyncio.sleep(0.6 if harness.backend == 'safari' else 0.2)


async def _get_node_by_id(session: BrowserSession, element_id: str):
	await session.get_browser_state_summary(include_screenshot=False, cached=False)
	index = await session.get_index_by_id(element_id)
	assert index is not None, f'Element #{element_id} not found in selector map'
	node = await session.get_element_by_index(index)
	assert node is not None, f'Node #{element_id} did not resolve from selector map'
	return index, node


@pytest.fixture(
	params=[
		pytest.param('chromium', id='chromium'),
		pytest.param('safari', id='safari', marks=pytest.mark.safari_live),
	]
)
async def contract_harness(
	request: pytest.FixtureRequest,
	parity_home_url: str,
	parity_run_token: str,
):
	backend = request.param
	safari_window_id: int | None = None
	safari_window_created = False
	if backend == 'safari':
		reason = _is_safari_available()
		if reason is not None:
			pytest.skip(reason)
		session = BrowserSession(automation_backend='safari', headless=False)
	else:
		session = BrowserSession(
			browser_profile=BrowserProfile(
				headless=True,
				user_data_dir=None,
				keep_alive=True,
			)
		)

	await session.start()
	capabilities = session.get_backend_capabilities()
	original_focus = session.agent_focus_target_id
	original_tab_ids = {tab.target_id for tab in await session.get_tabs_info()} if backend != 'safari' else set()

	try:
		if backend == 'safari':
			if capabilities.accessibility_permission != 'granted':
				pytest.skip('Safari live parity tests require Accessibility permission to open an isolated test window.')
			safari_window_id, safari_window_created = await _open_safari_test_window(session)
			await session.navigate_to(parity_home_url, new_tab=False)
		else:
			await session.navigate_to(parity_home_url)
		await _settle(ContractHarness(backend=backend, session=session, capabilities=capabilities))
		yield ContractHarness(backend=backend, session=session, capabilities=capabilities)
	finally:
		if backend == 'safari':
			try:
				if safari_window_id is not None and safari_window_created:
					try:
						await _close_safari_window(session, safari_window_id)
					except Exception:
						pass

				if original_focus:
					try:
						await session.switch_to_tab(original_focus)
					except Exception:
						pass
			finally:
				await session.kill()
		else:
			await session.kill()


@pytest.mark.asyncio
async def test_backend_parity_navigation_refresh_and_tab_contract(
	contract_harness: ContractHarness,
	parity_home_url: str,
	parity_page2_url: str,
):
	session = contract_harness.session

	assert await session.get_current_page_url() == parity_home_url
	assert await session.get_current_page_title() == 'Parity Home'
	assert await session.execute_javascript('document.title') == 'Parity Home'

	await session.execute_javascript("document.title = 'Mutated Title'; true;")
	assert await session.get_current_page_title() == 'Mutated Title'

	await session.refresh()
	await _settle(contract_harness)
	assert await session.get_current_page_title() == 'Parity Home'

	include_screenshot = contract_harness.capabilities.supports_screenshots and (
		contract_harness.backend != 'safari' or contract_harness.capabilities.screen_recording_permission == 'granted'
	)
	state = await session.get_browser_state_summary(include_screenshot=include_screenshot, cached=False)
	assert state.dom_state is not None
	assert state.page_info is not None
	if include_screenshot:
		assert state.screenshot
		assert len(await session.take_screenshot()) > 0

	first_target = session.agent_focus_target_id
	assert first_target is not None
	second_target = await session.create_new_tab(parity_page2_url)
	await _settle(contract_harness)
	assert second_target is not None
	assert await session.get_current_page_url() == parity_page2_url

	await session.switch_to_tab(first_target)
	await _settle(contract_harness)
	assert await session.get_current_page_url() == parity_home_url

	await session.close_tab(second_target)
	await _settle(contract_harness)
	assert all(tab.target_id != second_target for tab in await session.get_tabs_info())


@pytest.mark.asyncio
async def test_backend_parity_element_read_and_pointer_contract(contract_harness: ContractHarness):
	session = contract_harness.session
	tools = Tools()

	await session.execute_javascript(
		"""
		(() => {
			const input = document.getElementById('text-input');
			input.value = 'live value';
			input.dispatchEvent(new Event('input', { bubbles: true }));
			const text = document.getElementById('hover-target');
			text.textContent = 'Live text content';
			text.setAttribute('data-live', 'fresh');
			return true;
		})()
		"""
	)

	_, input_node = await _get_node_by_id(session, 'text-input')
	_, hover_node = await _get_node_by_id(session, 'hover-target')
	click_index, _ = await _get_node_by_id(session, 'click-target')
	_, double_node = await _get_node_by_id(session, 'double-target')
	_, right_node = await _get_node_by_id(session, 'right-target')

	assert await session.get_element_value(input_node) == 'live value'
	assert 'Live text content' in await session.get_element_text(hover_node)
	assert (await session.get_element_attributes(hover_node))['data-live'] == 'fresh'

	bbox = await session.get_element_bounding_box(hover_node)
	assert bbox['width'] > 0
	assert bbox['height'] > 0

	await session.hover_element(hover_node)
	await _settle(contract_harness)
	await tools.click(index=click_index, browser_session=session)
	await _settle(contract_harness)
	await session.double_click_element(double_node)
	await _settle(contract_harness)
	await session.right_click_element(right_node)
	await _settle(contract_harness)

	events = await session.execute_javascript('window.eventLog')
	assert isinstance(events, list)
	assert 'hover' in events
	assert 'pointerdown' in events
	assert 'mousedown' in events
	assert 'click' in events
	assert 'dblclick' in events
	assert 'rightclick' in events
	assert await session.execute_javascript("document.getElementById('click-status').textContent") == 'clicked'


@pytest.mark.asyncio
async def test_backend_parity_tool_navigation_and_dropdown_smoke(
	contract_harness: ContractHarness,
	parity_home_url: str,
	parity_page2_url: str,
):
	session = contract_harness.session
	tools = Tools()

	await tools.navigate(url=parity_page2_url, new_tab=False, browser_session=session)
	await _settle(contract_harness)
	assert await session.get_current_page_url() == parity_page2_url

	await tools.go_back(browser_session=session)
	await _settle(contract_harness)
	assert await session.get_current_page_url() == parity_home_url

	dropdown_index, dropdown_node = await _get_node_by_id(session, 'dropdown')
	options_result = await tools.dropdown_options(index=dropdown_index, browser_session=session)
	assert options_result.error is None
	assert options_result.extracted_content is not None
	assert 'Alpha' in options_result.extracted_content
	assert 'Beta' in options_result.extracted_content

	selection_result = await tools.select_dropdown(index=dropdown_index, text='Beta', browser_session=session)
	assert selection_result.error is None
	assert await session.execute_javascript("document.getElementById('selected-value').textContent") == 'beta'

	await session.scroll_to_text('Needle text target')
	await _settle(contract_harness)
	assert int(await session.execute_javascript('Math.round(window.scrollY)')) > 0
	assert await session.get_dropdown_options(dropdown_node) == {
		'Please select': '',
		'Alpha': 'alpha',
		'Beta': 'beta',
	}


@pytest.mark.asyncio
async def test_backend_parity_required_select_unlocks_primary_cta(contract_harness: ContractHarness):
	session = contract_harness.session
	tools = Tools()

	dropdown_index, dropdown_node = await _get_node_by_id(session, 'dropdown')
	continue_index, continue_node = await _get_node_by_id(session, 'continue-target')

	initial_attrs = await session.get_element_attributes(continue_node)
	assert initial_attrs.get('disabled') in {'', 'true', True, 'disabled'}

	# Clicking a disabled CTA must not silently advance the page state.
	disabled_click_result = await tools.click(index=continue_index, browser_session=session)
	await _settle(contract_harness)
	assert await session.execute_javascript("document.getElementById('continue-status').textContent") == ''
	if contract_harness.backend == 'safari':
		assert disabled_click_result.error is not None
		assert 'Cannot click disabled element' in disabled_click_result.error

	fresh_dropdown_index, _ = await _get_node_by_id(session, 'dropdown')
	await tools.select_dropdown(index=fresh_dropdown_index, text='Alpha', browser_session=session)
	await _settle(contract_harness)
	assert await session.execute_javascript("document.getElementById('selected-value').textContent") == 'alpha'

	fresh_continue_index, fresh_continue_node = await _get_node_by_id(session, 'continue-target')
	refreshed_attrs = await session.get_element_attributes(fresh_continue_node)
	assert refreshed_attrs.get('disabled') in {None, '', False}

	enabled_click_result = await tools.click(index=fresh_continue_index, browser_session=session)
	assert enabled_click_result.error is None
	await _settle(contract_harness)
	assert await session.execute_javascript("document.getElementById('continue-status').textContent") == 'continued'


@pytest.mark.asyncio
async def test_backend_parity_file_upload_contract(
	contract_harness: ContractHarness,
	parity_home_url: str,
	tmp_path: Path,
):
	session = contract_harness.session
	await session.navigate_to(parity_home_url)
	await _settle(contract_harness)
	_, file_node = await _get_node_by_id(session, 'file-input')

	upload_path = tmp_path / 'parity-upload.txt'
	upload_path.write_text('backend parity upload\n', encoding='utf-8')

	await session.upload_file_to_element(file_node, str(upload_path))
	await asyncio.sleep(1.0 if contract_harness.backend == 'safari' else 0.3)

	assert await session.execute_javascript("document.getElementById('file-name').textContent") == upload_path.name

	# Also ensure the file-upload tool path still works against the same page contract.
	await session.get_browser_state_summary(include_screenshot=False, cached=False)
	file_index = await session.get_index_by_id('file-input')
	assert file_index is not None
	tools = Tools()
	file_system = FileSystem(tmp_path)
	tool_result = await tools.upload_file(
		index=file_index,
		path=str(upload_path),
		browser_session=session,
		available_file_paths=[str(upload_path)],
		file_system=file_system,
	)
	assert tool_result.error is None
