"""Tests for Safari backend helpers."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from browser_use import Agent
from browser_use.browser.backends.base import BackendCapabilityReport
from browser_use.browser.backends.safari_backend import SafariRealProfileBackend, _run_applescript_sync, _run_jxa_sync
from browser_use.browser.events import NavigateToUrlEvent
from browser_use.browser.safari.capabilities import SafariCapabilityReport, probe_safari_environment
from browser_use.browser.safari.profiles import SafariProfileStore
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserError, BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import DOMRect, NodeType
from browser_use.filesystem.file_system import FileSystem
from browser_use.llm import BaseChatModel
from browser_use.tools.service import Tools


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


def test_safari_state_extraction_includes_interactive_labels():
	"""Safari DOM extraction should include visible labels and control state for form controls."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)

	script = backend._state_extraction_script()

	assert "'label'" in script
	assert "el.tagName === 'LABEL' && !el.control && !el.querySelector('input,select,textarea')" in script
	assert 'const contextualLabelFor = el => {' in script
	assert "'selected-text': el.matches('select') ? selectValueText(el).slice(0, 200) : ''" in script
	assert "disabled: ('disabled' in el && el.disabled) ? 'true' : ''" in script
	assert "required: ('required' in el && el.required) ? 'true' : ''" in script
	assert "checked: ('checked' in el && el.checked) ? 'true' : ''" in script


def test_safari_sparse_state_detection_requires_non_blank_url_and_empty_content():
	"""Sparse-state recovery should trigger only for non-blank pages with no title, controls, or text."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)

	assert backend._state_needs_recovery(
		{
			'url': 'https://apple.com/mac-pro',
			'title': '',
			'elements': [],
			'textBlocks': [],
		}
	)
	assert not backend._state_needs_recovery(
		{
			'url': 'about:blank',
			'title': '',
			'elements': [],
			'textBlocks': [],
		}
	)
	assert not backend._state_needs_recovery(
		{
			'url': 'https://apple.com/mac-pro',
			'title': 'Mac Pro',
			'elements': [],
			'textBlocks': [],
		}
	)


def test_safari_agent_defaults_to_single_action_steps():
	"""Safari agents should default to one action per step on volatile real-profile pages."""
	llm = AsyncMock(spec=BaseChatModel)
	llm.model = 'mock-llm'
	llm.provider = 'mock'
	llm.name = 'mock-llm'
	llm.model_name = 'mock-llm'
	llm._verified_api_keys = True

	safari_session = BrowserSession(automation_backend='safari', headless=False)
	safari_agent = Agent(task='Test task', llm=llm, browser_session=safari_session)
	explicit_agent = Agent(task='Test task', llm=llm, browser_session=safari_session, max_actions_per_step=3)
	chromium_agent = Agent(task='Test task', llm=llm, browser_session=BrowserSession(headless=False))

	assert safari_agent.settings.max_actions_per_step == 1
	assert explicit_agent.settings.max_actions_per_step == 3
	assert chromium_agent.settings.max_actions_per_step == 5


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
	assert 'Cannot click disabled element.' in script
	assert 'unresolvedControls' in script
	assert 'pointerdown' in script
	assert 'mousedown' in script
	assert 'mouseup' in script
	assert 'target.click()' in script
	assert 'closest(interactiveSelector)' in script
	refresh_focus_target.assert_awaited_once()


@pytest.mark.asyncio
async def test_safari_click_element_propagates_disabled_validation_error():
	"""Safari click helper should surface disabled controls as validation errors."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	object.__setattr__(backend, '_ensure_profile_window', AsyncMock(return_value={'windowId': 123}))
	refresh_focus_target = AsyncMock(return_value=None)
	evaluate_javascript = AsyncMock(return_value={'validation_error': 'Cannot click disabled element.'})
	object.__setattr__(backend, '_refresh_focus_target', refresh_focus_target)
	object.__setattr__(backend, 'evaluate_javascript', evaluate_javascript)
	node = cast(Any, SimpleNamespace(backend_node_id=19))

	result = await backend.click_element(node)

	assert result == {'validation_error': 'Cannot click disabled element.'}
	refresh_focus_target.assert_awaited_once()


@pytest.mark.asyncio
async def test_safari_get_browser_state_summary_refreshes_sparse_page_once():
	"""Safari state capture should refresh once when the page comes back blank/sparse."""
	session = BrowserSession(automation_backend='safari', headless=False)
	backend = SafariRealProfileBackend(session)
	dom_state = SimpleNamespace(selector_map={}, llm_representation=lambda: '')
	first_state = {'url': 'https://apple.com/mac-pro', 'title': '', 'elements': [], 'textBlocks': []}
	second_state = {
		'url': 'https://apple.com/mac-pro',
		'title': 'Mac Pro',
		'elements': [
			{'id': 1, 'tag': 'button', 'type': '', 'role': '', 'text': 'Continue', 'label': '', 'rect': None, 'attributes': {}}
		],
		'textBlocks': [{'tag': 'h1', 'text': 'Mac Pro', 'rect': None}],
		'page': {
			'viewportWidth': 1440,
			'viewportHeight': 900,
			'pageWidth': 1440,
			'pageHeight': 2400,
			'scrollX': 0,
			'scrollY': 0,
			'pixelsAbove': 0,
			'pixelsBelow': 1500,
		},
	}
	object.__setattr__(
		backend,
		'_ensure_profile_window',
		AsyncMock(return_value={'windowId': 12, 'currentTabIndex': 3, 'url': 'https://apple.com/mac-pro', 'title': 'Mac Pro'}),
	)
	object.__setattr__(backend, 'evaluate_javascript', AsyncMock(side_effect=[first_state, second_state]))
	object.__setattr__(backend, '_build_serialized_dom_state', Mock(return_value=dom_state))
	object.__setattr__(
		backend,
		'get_tabs',
		AsyncMock(
			return_value=[
				TabInfo(target_id='safari:12:3', url='https://apple.com/mac-pro', title='Mac Pro', parent_target_id=None)
			]
		),
	)
	object.__setattr__(backend, 'take_screenshot', AsyncMock(return_value=b''))
	object.__setattr__(backend, 'refresh', AsyncMock(return_value=None))
	object.__setattr__(session, '_backend', backend)

	state = await backend.get_browser_state_summary(include_screenshot=False)

	assert state.title == 'Mac Pro'
	cast(Any, backend.refresh).assert_awaited_once()
	assert cast(Any, backend.evaluate_javascript).await_count == 2


@pytest.mark.asyncio
async def test_safari_close_tab_uses_null_safe_tab_lookup():
	"""Safari tab close helper should tolerate transient null tab collections."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	run_jxa = AsyncMock(return_value='')
	refresh_focus_target = AsyncMock(return_value=None)
	object.__setattr__(backend, '_run_jxa', run_jxa)
	object.__setattr__(backend, '_refresh_focus_target', refresh_focus_target)

	await backend.close_tab('safari:12:3')

	run_jxa.assert_awaited_once()
	await_args = run_jxa.await_args
	assert await_args is not None
	script = await_args.args[0]
	assert 'Array.from(win.tabs() || []).filter(Boolean)' in script
	assert 'const tabs = Array.from(win.tabs() || []).filter(Boolean);' in script
	assert 'if (tabs.length <= 1) {' in script
	assert 'tab.url = "about:blank";' in script
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
async def test_safari_scroll_to_text_prefers_nearest_relevant_match():
	"""Safari text scrolling should rank nearby interactive matches ahead of generic container text."""
	session = cast(BrowserSession, SimpleNamespace(logger=Mock()))
	backend = SafariRealProfileBackend(session)
	evaluate_javascript = AsyncMock(return_value=True)
	object.__setattr__(backend, 'evaluate_javascript', evaluate_javascript)

	await backend.scroll_to_text('AppleCare+', direction='down')

	evaluate_javascript.assert_awaited_once()
	await_args = evaluate_javascript.await_args
	assert await_args is not None
	script = await_args.args[0]
	assert 'directionDistance' in script
	assert 'isInteractive' in script
	assert 'textLength' in script
	assert 'node = candidates[0].el' in script


@pytest.mark.asyncio
async def test_safari_tab_resolution_prefers_same_window_non_popup_candidate():
	"""Ambiguous short Safari tab ids should prefer same-window non-popup tabs."""
	session = BrowserSession(automation_backend='safari', headless=False)
	session.agent_focus_target_id = 'safari:12:4'

	async def fake_get_tabs():
		return [
			TabInfo(target_id='safari:112:3', url='https://support.apple.com/mac-pro', title='Support', parent_target_id=None),
			TabInfo(target_id='safari:12:3', url='https://www.apple.com/mac-pro/', title='Mac Pro', parent_target_id=None),
		]

	object.__setattr__(session, 'get_tabs', fake_get_tabs)

	resolved = await session.get_target_id_from_tab_id('12:3')

	assert resolved == 'safari:12:3'


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


@pytest.mark.asyncio
async def test_safari_tools_use_cross_backend_javascript_helpers(tmp_path: Path):
	"""Safari should use evaluate_javascript helpers for JS-based tools instead of raw CDP."""

	class FakeCapabilities:
		supports_cdp = False

	class FakeSafariSession:
		def __init__(self):
			self.calls: list[str] = []

		def get_backend_capabilities(self):
			return FakeCapabilities()

		async def evaluate_javascript(self, script: str):
			self.calls.append(script)
			if 'find_elements error' in script:
				return {
					'elements': [{'index': 0, 'tag': 'article', 'text': 'DramaAlert'}],
					'total': 1,
					'showing': 1,
				}
			if 'search_page error' in script:
				return {
					'matches': [{'context': 'DramaAlert bookmarked post', 'element_path': 'body > article'}],
					'total': 1,
					'has_more': False,
				}
			if 'document.title' in script:
				return 'Safari Page'
			return {'status': 'ok'}

		@property
		def cdp_client(self):
			raise AssertionError('Safari JS tools should not access cdp_client')

	tools = Tools()
	session = FakeSafariSession()

	find_result = await tools.find_elements(selector='article', browser_session=session)
	search_result = await tools.search_page(pattern='DramaAlert', browser_session=session)
	eval_result = await tools.evaluate(code='document.title', browser_session=session)
	save_result = await tools.save_as_pdf(
		browser_session=session,
		file_system=FileSystem(str(tmp_path)),
	)

	assert find_result.error is None
	assert find_result.extracted_content is not None
	assert 'DramaAlert' in find_result.extracted_content
	assert search_result.error is None
	assert search_result.extracted_content is not None
	assert 'DramaAlert bookmarked post' in search_result.extracted_content
	assert eval_result.error is None
	assert eval_result.extracted_content == 'Safari Page'
	assert save_result.error == (
		'save_as_pdf is not supported with the Safari real-profile backend. Use screenshot or extract page content instead.'
	)
	assert any('find_elements error' in call for call in session.calls)
	assert any('search_page error' in call for call in session.calls)


@pytest.mark.asyncio
async def test_safari_scroll_uses_shared_page_metrics_instead_of_cdp():
	"""Scroll should resolve viewport height from browser state so Safari does not depend on CDP."""

	class FakeEvent:
		def __init__(self, payload):
			self.payload = payload

		def __await__(self):
			async def _done():
				return self

			return _done().__await__()

		async def event_result(self, raise_if_any=True, raise_if_none=False):
			return None

	class FakeEventBus:
		def __init__(self):
			self.events: list[Any] = []

		def dispatch(self, event):
			self.events.append(event)
			return FakeEvent(event)

	class FakeSafariSession:
		def __init__(self):
			self.event_bus = FakeEventBus()

		def get_backend_capabilities(self):
			return SimpleNamespace(supports_cdp=False)

		async def get_element_by_index(self, index):
			return None

		async def get_browser_state_summary(self, include_screenshot=False, cached=False):
			dom_state = cast(Any, SimpleNamespace(selector_map={}, llm_representation=lambda: ''))
			return BrowserStateSummary(
				dom_state=dom_state,
				url='https://example.com',
				title='Example',
				tabs=[],
				page_info=PageInfo(
					viewport_width=1440,
					viewport_height=720,
					page_width=1440,
					page_height=2400,
					scroll_x=0,
					scroll_y=380,
					pixels_above=380,
					pixels_below=1300,
					pixels_left=0,
					pixels_right=0,
				),
			)

		async def get_or_create_cdp_session(self):
			raise AssertionError('Scroll should not require CDP for Safari')

	tools = Tools()
	session = FakeSafariSession()

	result = await tools.scroll(browser_session=session)

	assert result.error is None
	assert session.event_bus.events
	assert session.event_bus.events[0].amount == 720


@pytest.mark.asyncio
async def test_safari_upload_fallback_uses_shared_scroll_state(tmp_path: Path):
	"""Upload fallback should pick the nearest file input using shared page metrics, not CDP."""

	class FakeEvent:
		def __init__(self, node):
			self.node = node

		def __await__(self):
			async def _done():
				return self

			return _done().__await__()

		async def event_result(self, raise_if_any=True, raise_if_none=False):
			return None

	class FakeEventBus:
		def __init__(self):
			self.last_node = None

		def dispatch(self, event):
			self.last_node = event.node
			return FakeEvent(event.node)

	class FakeSafariSession:
		def __init__(self, selector_map):
			self.event_bus = FakeEventBus()
			self.downloaded_files: list[str] = []
			self.is_local = True
			self._selector_map = selector_map

		def get_backend_capabilities(self):
			return SimpleNamespace(supports_cdp=False)

		async def get_selector_map(self):
			return self._selector_map

		def is_file_input(self, node):
			return node.attributes.get('type') == 'file'

		async def highlight_interaction_element(self, node):
			return None

		async def get_browser_state_summary(self, include_screenshot=False, cached=False):
			dom_state = cast(Any, SimpleNamespace(selector_map=self._selector_map, llm_representation=lambda: ''))
			return BrowserStateSummary(
				dom_state=dom_state,
				url='https://example.com/upload',
				title='Upload',
				tabs=[],
				page_info=PageInfo(
					viewport_width=1440,
					viewport_height=900,
					page_width=1440,
					page_height=3000,
					scroll_x=0,
					scroll_y=860,
					pixels_above=860,
					pixels_below=1240,
					pixels_left=0,
					pixels_right=0,
				),
			)

		async def get_or_create_cdp_session(self):
			raise AssertionError('Upload fallback should not require CDP for Safari')

	file_path = tmp_path / 'upload.txt'
	file_path.write_text('payload')

	target_node = SimpleNamespace(
		node_id=1,
		backend_node_id=101,
		session_id=None,
		frame_id=None,
		target_id='safari:12:3',
		node_type=NodeType.ELEMENT_NODE,
		node_name='BUTTON',
		node_value='',
		attributes={'type': 'button'},
		is_scrollable=False,
		is_visible=True,
		children_nodes=[],
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		ax_node=None,
		snapshot_node=None,
		absolute_position=DOMRect(x=0, y=1200, width=10, height=10),
	)
	nearby_input = SimpleNamespace(
		node_id=2,
		backend_node_id=102,
		session_id=None,
		frame_id=None,
		target_id='safari:12:3',
		node_type=NodeType.ELEMENT_NODE,
		node_name='INPUT',
		node_value='',
		attributes={'type': 'file'},
		is_scrollable=False,
		is_visible=True,
		children_nodes=[],
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		ax_node=None,
		snapshot_node=None,
		absolute_position=DOMRect(x=0, y=120, width=10, height=10),
	)
	visible_input = SimpleNamespace(
		node_id=3,
		backend_node_id=103,
		session_id=None,
		frame_id=None,
		target_id='safari:12:3',
		node_type=NodeType.ELEMENT_NODE,
		node_name='INPUT',
		node_value='',
		attributes={'type': 'file'},
		is_scrollable=False,
		is_visible=True,
		children_nodes=[],
		content_document=None,
		shadow_root_type=None,
		shadow_roots=None,
		parent_node=None,
		ax_node=None,
		snapshot_node=None,
		absolute_position=DOMRect(x=0, y=900, width=10, height=10),
	)
	selector_map = {1: target_node, 2: nearby_input, 3: visible_input}

	tools = Tools()
	session = FakeSafariSession(selector_map)

	result = await tools.upload_file(
		index=1,
		path=str(file_path),
		browser_session=session,
		available_file_paths=[str(file_path)],
		file_system=FileSystem(str(tmp_path)),
	)

	assert result.error is None
	assert session.event_bus.last_node is not None
	assert session.event_bus.last_node.backend_node_id == visible_input.backend_node_id


def test_safari_recovery_tab_prefers_non_popup_content():
	"""Safari recovery should prefer a real content tab over a support/help tab."""
	session = BrowserSession(automation_backend='safari', headless=False)
	session.agent_focus_target_id = 'safari:7:4'
	tabs = [
		TabInfo(target_id='safari:7:4', url='https://support.apple.com/mac-pro', title='Mac Pro Support', parent_target_id=None),
		TabInfo(
			target_id='safari:7:2', url='https://www.apple.com/shop/buy-mac/mac-pro', title='Buy Mac Pro', parent_target_id=None
		),
		TabInfo(target_id='safari:8:1', url='about:blank', title='', parent_target_id=None),
	]

	preferred = session._pick_safari_recovery_tab(tabs)

	assert preferred is not None
	assert preferred.target_id == 'safari:7:2'


@pytest.mark.asyncio
async def test_safari_tab_id_resolution_prefers_non_popup_candidate_when_suffix_is_ambiguous():
	"""Ambiguous Safari tab suffixes should resolve to the strongest non-popup candidate."""
	session = BrowserSession(automation_backend='safari', headless=False)
	session.agent_focus_target_id = 'safari:11:7'
	object.__setattr__(session, 'session_manager', None)
	object.__setattr__(
		session,
		'get_tabs',
		AsyncMock(
			return_value=[
				TabInfo(
					target_id='safari:11:2',
					url='https://support.apple.com/store',
					title='Apple Store Support',
					parent_target_id=None,
				),
				TabInfo(
					target_id='safari:1:2',
					url='https://www.apple.com/shop/buy-mac/mac-pro',
					title='Buy Mac Pro',
					parent_target_id=None,
				),
			]
		),
	)

	target_id = await session.get_target_id_from_tab_id('1:2')

	assert target_id == 'safari:1:2'


def _make_browser_state(
	url: str,
	*,
	title: str = '',
	selector_count: int = 0,
	dom_text: str = '',
	screenshot: str | None = None,
) -> BrowserStateSummary:
	dom_state = cast(
		Any,
		SimpleNamespace(
			selector_map={index: object() for index in range(selector_count)},
			llm_representation=lambda: dom_text,
		),
	)
	return BrowserStateSummary(
		dom_state=dom_state,
		url=url,
		title=title,
		tabs=[TabInfo(target_id='safari:1:1', url=url, title=title, parent_target_id=None)],
		screenshot=screenshot,
		page_info=PageInfo(
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
		),
	)


@pytest.mark.asyncio
async def test_safari_state_summary_refreshes_once_when_state_is_sparse():
	"""Safari session should refresh once before returning a suspiciously blank state."""
	session = BrowserSession(automation_backend='safari', headless=False)
	sparse_state = _make_browser_state('https://www.apple.com/shop/buy-mac/mac-pro')
	healthy_state = _make_browser_state(
		'https://www.apple.com/shop/buy-mac/mac-pro',
		title='Mac Pro',
		selector_count=5,
		dom_text='Mac Pro Buy button Configure AppleCare',
	)
	backend = SimpleNamespace(
		get_browser_state_summary=AsyncMock(side_effect=[sparse_state, healthy_state]),
		refresh=AsyncMock(return_value=None),
	)
	object.__setattr__(session, '_backend', backend)

	state = await session.get_browser_state_summary(include_screenshot=False, cached=False)

	assert state is healthy_state
	backend.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_safari_state_summary_does_not_refresh_sparse_about_blank():
	"""Sparse about:blank Safari state is expected and should not trigger a refresh."""
	session = BrowserSession(automation_backend='safari', headless=False)
	blank_state = _make_browser_state('about:blank')
	backend = SimpleNamespace(
		get_browser_state_summary=AsyncMock(return_value=blank_state),
		refresh=AsyncMock(return_value=None),
	)
	object.__setattr__(session, '_backend', backend)

	state = await session.get_browser_state_summary(include_screenshot=False, cached=False)

	assert state is blank_state
	backend.refresh.assert_not_awaited()
