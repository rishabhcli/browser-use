"""Tests for remaining Safari session utility/helper APIs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.events import BrowserStateRequestEvent
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType, SerializedDOMState
from safari_session.session import SafariBrowserSession


def _node(name: str, attrs: dict[str, str] | None = None) -> EnhancedDOMTreeNode:
	return EnhancedDOMTreeNode(
		node_id=1,
		backend_node_id=1,
		node_type=NodeType.ELEMENT_NODE,
		node_name=name,
		node_value='',
		attributes=attrs or {},
		is_scrollable=False,
		is_visible=True,
		absolute_position=DOMRect(x=0, y=0, width=1, height=1),
		target_id='safari-target',
		frame_id='main',
		session_id='safari',
		content_document=None,
		shadow_root_type=None,
		shadow_roots=[],
		parent_node=None,
		children_nodes=[],
		ax_node=None,
		snapshot_node=None,
	)


def _summary(screenshot: str | None = None) -> BrowserStateSummary:
	return BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={}),
		url='https://example.com',
		title='Example',
		tabs=[TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)],
		screenshot=screenshot,
	)


@pytest.mark.asyncio
async def test_is_file_input_covers_true_and_false_cases() -> None:
	session = SafariBrowserSession()
	try:
		assert session.is_file_input(_node('INPUT', {'type': 'file'})) is True
		assert session.is_file_input(_node('INPUT', {'type': 'text'})) is False
		assert session.is_file_input(_node('DIV', {'type': 'file'})) is False
		assert session.is_file_input('not-a-node') is False
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_get_browser_state_summary_returns_cached_without_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	cached = _summary(screenshot='cached-image')
	session._cached_browser_state_summary = cached

	def fail_dispatch(event: BrowserStateRequestEvent) -> Any:
		del event
		raise AssertionError('dispatch should not be called for valid cached summary')

	monkeypatch.setattr(session.event_bus, 'dispatch', fail_dispatch)
	try:
		result = await session.get_browser_state_summary(include_screenshot=True, cached=True)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert result is cached


@pytest.mark.asyncio
async def test_get_browser_state_summary_dispatches_when_cached_screenshot_missing(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	session = SafariBrowserSession()
	session._cached_browser_state_summary = _summary(screenshot=None)
	fresh = _summary(screenshot='fresh-image')
	dispatched: list[BrowserStateRequestEvent] = []

	class _FakeEvent:
		async def event_result(self, raise_if_none: bool = True, raise_if_any: bool = True) -> BrowserStateSummary:
			del raise_if_none, raise_if_any
			return fresh

	def fake_dispatch(event: BrowserStateRequestEvent) -> _FakeEvent:
		dispatched.append(event)
		return _FakeEvent()

	monkeypatch.setattr(session.event_bus, 'dispatch', fake_dispatch)
	try:
		result = await session.get_browser_state_summary(include_screenshot=True, cached=True)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert result is fresh
	assert dispatched
	assert dispatched[0].include_screenshot is True
	assert dispatched[0].include_dom is True


@pytest.mark.asyncio
async def test_get_browser_state_summary_returns_cached_when_include_screenshot_false(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	session = SafariBrowserSession()
	cached = _summary(screenshot=None)
	session._cached_browser_state_summary = cached

	def fail_dispatch(event: BrowserStateRequestEvent) -> Any:
		del event
		raise AssertionError('dispatch should not be called when include_screenshot=False and cache exists')

	monkeypatch.setattr(session.event_bus, 'dispatch', fail_dispatch)
	try:
		result = await session.get_browser_state_summary(include_screenshot=False, cached=True)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert result is cached


@pytest.mark.asyncio
async def test_get_state_as_text_uses_dom_llm_representation(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_get_browser_state_summary(
		self: SafariBrowserSession, include_screenshot: bool = True, cached: bool = False, include_recent_events: bool = False
	) -> Any:
		del self, include_screenshot, cached, include_recent_events
		return SimpleNamespace(dom_state=SimpleNamespace(llm_representation=lambda: '[1] <button>Go</button>'))

	monkeypatch.setattr(SafariBrowserSession, 'get_browser_state_summary', fake_get_browser_state_summary)
	try:
		text = await session.get_state_as_text()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert text == '[1] <button>Go</button>'


@pytest.mark.asyncio
async def test_export_storage_state_writes_output_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	session = SafariBrowserSession()
	session.driver._driver = object()
	output_path = tmp_path / 'exported-state.json'

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_get_cookies() -> list[dict[str, Any]]:
		return [{'name': 'sid', 'value': 'abc'}]

	async def fake_execute_js(script: str, *args: Any) -> dict[str, Any]:
		del script, args
		return {'theme': 'dark'}

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session.driver, 'get_cookies', fake_get_cookies)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		state = await session.export_storage_state(output_path=output_path)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert state['cookies'] == [{'name': 'sid', 'value': 'abc'}]
	assert state['localStorage'] == {'theme': 'dark'}
	assert output_path.exists()
	on_disk = json.loads(output_path.read_text())
	assert on_disk['localStorage'] == {'theme': 'dark'}


@pytest.mark.asyncio
async def test_export_storage_state_normalizes_non_dict_local_storage(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	session.driver._driver = object()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_get_cookies() -> list[dict[str, Any]]:
		return []

	async def fake_execute_js(script: str, *args: Any) -> str:
		del script, args
		return 'invalid-storage-payload'

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session.driver, 'get_cookies', fake_get_cookies)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		state = await session.export_storage_state()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert state['localStorage'] == {}


@pytest.mark.asyncio
async def test_clear_cookies_calls_driver_and_invalidates_dom_cache(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	calls = 0
	session._cached_selector_map = {1: _node('DIV')}
	session._ref_by_backend_id = {
		1: safari_session_module._SafariElementRef(
			backend_node_id=1,
			stable_id='stable-1',
			tag_name='div',
			text_content='',
			attributes={},
			css_selector='#id',
			xpath='//*[@id="id"]',
			absolute_position=DOMRect(x=0, y=0, width=10, height=10),
		)
	}
	session._cached_browser_state_summary = _summary(screenshot='cached')

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_clear_cookies() -> None:
		nonlocal calls
		calls += 1

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session.driver, 'clear_cookies', fake_clear_cookies)

	try:
		await session.clear_cookies()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert calls == 1
	assert session._cached_browser_state_summary is None
	assert session._cached_selector_map == {}
	assert session._ref_by_backend_id == {}


@pytest.mark.asyncio
async def test_noop_helper_methods_return_none() -> None:
	session = SafariBrowserSession()
	try:
		assert await session.send_demo_mode_log('message', level='info', metadata={'a': 1}) is None
		assert await session.highlight_interaction_element(_node('BUTTON')) is None
		assert await session.highlight_coordinate_click(10, 20) is None
		assert await session.remove_highlights() is None
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_downloaded_files_property_returns_copy() -> None:
	session = SafariBrowserSession()
	session._downloaded_files = ['/tmp/a.pdf']
	try:
		exported = session.downloaded_files
		exported.append('/tmp/other.pdf')
		assert session._downloaded_files == ['/tmp/a.pdf']
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_cdp_client_property_creates_stable_shim_instance() -> None:
	session = SafariBrowserSession()
	try:
		first = session.cdp_client
		second = session.cdp_client
		assert first is second
		assert session._cdp_shim is first
		assert session._cdp_client_root is first
	finally:
		await session.event_bus.stop(clear=True, timeout=5)
