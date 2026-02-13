"""Tests for Safari session compatibility/helper APIs and CDP shims."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType, SerializedDOMState
from safari_session.session import SafariBrowserSession


def _make_node(
	backend_node_id: int,
	*,
	node_name: str = 'DIV',
	attrs: dict[str, str] | None = None,
) -> EnhancedDOMTreeNode:
	return EnhancedDOMTreeNode(
		node_id=backend_node_id,
		backend_node_id=backend_node_id,
		node_type=NodeType.ELEMENT_NODE,
		node_name=node_name,
		node_value='',
		attributes=attrs or {},
		is_scrollable=False,
		is_visible=True,
		absolute_position=DOMRect(x=1, y=2, width=30, height=12),
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


@pytest.mark.asyncio
async def test_get_target_id_from_tab_id_matches_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	target_id = 'safari-target-abc123'
	session._target_to_handle = {target_id: 'handle-1'}

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		return []

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)

	try:
		found = await session.get_target_id_from_tab_id('c123')
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert found == target_id


@pytest.mark.asyncio
async def test_get_target_id_from_tab_id_raises_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	session._target_to_handle = {'safari-target-one': 'h1'}

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		return []

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)

	try:
		with pytest.raises(ValueError, match='No tab found with tab_id suffix'):
			await session.get_target_id_from_tab_id('xxxx')
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_selector_and_index_helpers_use_cached_selector_map() -> None:
	session = SafariBrowserSession()
	node_a = _make_node(1, attrs={'id': 'email', 'class': 'field primary'})
	node_b = _make_node(2, attrs={'id': 'password', 'class': 'field secure'})
	session.update_cached_selector_map({1: node_a, 2: node_b})

	try:
		selector_map = await session.get_selector_map()
		assert selector_map[1].attributes['id'] == 'email'
		assert await session.get_element_by_index(2) is node_b
		assert await session.get_dom_element_by_index(1) is node_a
		assert await session.get_index_by_id('password') == 2
		assert await session.get_index_by_class('primary') == 1
		assert await session.get_index_by_class('missing') is None
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_get_selector_map_fetches_from_browser_state_when_cache_empty(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	node = _make_node(9, attrs={'id': 'fetched'})
	state = BrowserStateSummary(
		dom_state=SerializedDOMState(_root=None, selector_map={9: node}),
		url='https://example.com',
		title='Example',
		tabs=[TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)],
		screenshot=None,
	)

	async def fake_get_browser_state_summary(
		self: SafariBrowserSession, include_screenshot: bool = True, cached: bool = False, include_recent_events: bool = False
	) -> BrowserStateSummary:
		del self
		del include_screenshot, cached, include_recent_events
		return state

	monkeypatch.setattr(SafariBrowserSession, 'get_browser_state_summary', fake_get_browser_state_summary)
	try:
		selector_map = await session.get_selector_map()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert selector_map == {9: node}


@pytest.mark.asyncio
async def test_get_most_recently_opened_target_id_and_empty_case(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_get_tabs_non_empty(self: SafariBrowserSession) -> list[TabInfo]:
		del self
		return [
			TabInfo(url='https://a.example', title='A', target_id='target-a', parent_target_id=None),
			TabInfo(url='https://b.example', title='B', target_id='target-b', parent_target_id=None),
		]

	monkeypatch.setattr(SafariBrowserSession, 'get_tabs', fake_get_tabs_non_empty)
	assert await session.get_most_recently_opened_target_id() == 'target-b'

	async def fake_get_tabs_empty(self: SafariBrowserSession) -> list[TabInfo]:
		del self
		return []

	monkeypatch.setattr(SafariBrowserSession, 'get_tabs', fake_get_tabs_empty)
	with pytest.raises(RuntimeError, match='No tabs available'):
		await session.get_most_recently_opened_target_id()
	await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_take_screenshot_returns_bytes_and_writes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	session = SafariBrowserSession()
	data = b'\x89PNGfake'
	encoded = base64.b64encode(data).decode('ascii')

	async def fake_screenshot(self: SafariBrowserSession) -> str:
		del self
		return encoded

	monkeypatch.setattr(SafariBrowserSession, 'screenshot', fake_screenshot)
	out_path = tmp_path / 'screen.png'

	try:
		written = await session.take_screenshot(path=str(out_path))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert written == data
	assert out_path.read_bytes() == data


@pytest.mark.asyncio
async def test_get_or_create_cdp_session_switches_focus_when_target_and_handle_exist(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	session = SafariBrowserSession()
	target_id = 'target-focus'
	session._target_to_handle = {target_id: 'handle-focus'}
	session._cdp_shim = safari_session_module._CDPClientShim(session)
	switch_calls: list[str] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_switch_to_handle(handle: str) -> Any:
		switch_calls.append(handle)
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session.driver, 'switch_to_handle', fake_switch_to_handle)

	try:
		cdp_session = await session.get_or_create_cdp_session(target_id=target_id, focus=True)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert switch_calls == ['handle-focus']
	assert session.agent_focus_target_id == target_id
	assert cdp_session.target_id == target_id
	assert cdp_session.session_id == 'safari-session'


@pytest.mark.asyncio
async def test_get_or_create_cdp_session_refreshes_tabs_when_no_target(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	session._cdp_shim = safari_session_module._CDPClientShim(session)
	refresh_calls = 0
	session.agent_focus_target_id = 'target-current'

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_refresh_tabs() -> list[TabInfo]:
		nonlocal refresh_calls
		refresh_calls += 1
		return []

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)

	try:
		cdp_session = await session.get_or_create_cdp_session(target_id=None, focus=True)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert refresh_calls == 1
	assert cdp_session.target_id == 'target-current'


@pytest.mark.asyncio
async def test_runtime_evaluate_sync_success_and_error_paths(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_execute_js(expression: str, *args: Any) -> Any:
		assert expression == 'return eval(arguments[0]);'
		assert args == ('1 + 2',)
		return 3

	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		ok = await session.cdp_client.send.Runtime.evaluate({'expression': '1 + 2'})
		assert ok['result']['value'] == 3
		assert ok['result']['type'] == 'int'

		async def fake_execute_js_raises(expression: str, *args: Any) -> Any:
			del expression, args
			raise RuntimeError('eval failed')

		monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js_raises)
		error_result = await session.cdp_client.send.Runtime.evaluate({'expression': 'bad()'})
		assert error_result['result']['type'] == 'undefined'
		assert 'exceptionDetails' in error_result
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_runtime_evaluate_await_promise_success_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_execute_async_js_ok(expression: str, *args: Any) -> dict[str, Any]:
		del expression
		assert args == ('Promise.resolve(5)',)
		return {'ok': True, 'value': 5}

	monkeypatch.setattr(session.driver, 'execute_async_js', fake_execute_async_js_ok)

	try:
		ok = await session.cdp_client.send.Runtime.evaluate(
			{'expression': 'Promise.resolve(5)', 'awaitPromise': True}
		)
		assert ok['result']['value'] == 5

		async def fake_execute_async_js_fail(expression: str, *args: Any) -> dict[str, Any]:
			del expression, args
			return {'ok': False, 'error': 'promise rejected'}

		monkeypatch.setattr(session.driver, 'execute_async_js', fake_execute_async_js_fail)
		failed = await session.cdp_client.send.Runtime.evaluate(
			{'expression': 'Promise.reject()', 'awaitPromise': True}
		)
		assert failed['result']['type'] == 'undefined'
		assert failed['exceptionDetails']['text'] == 'promise rejected'
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_page_get_layout_metrics_maps_values_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_execute_js(_expression: str) -> dict[str, Any]:
		return {
			'viewport_width': 1280,
			'viewport_height': 720,
			'page_width': 2000,
			'page_height': 3000,
			'scroll_x': 10,
			'scroll_y': 20,
		}

	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		metrics = await session.cdp_client.send.Page.getLayoutMetrics()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert metrics['cssVisualViewport']['clientWidth'] == 1280
	assert metrics['cssVisualViewport']['clientHeight'] == 720
	assert metrics['cssVisualViewport']['offsetX'] == 10
	assert metrics['cssVisualViewport']['offsetY'] == 20
	assert metrics['cssContentSize']['width'] == 2000
	assert metrics['cssContentSize']['height'] == 3000
