"""Tests for Safari interaction fallbacks when selector re-location fails."""

from typing import Any

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.events import ClickElementEvent, TypeTextEvent
from browser_use.browser.views import TabInfo
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType
from safari_session import SafariBrowserSession


def _make_node(backend_node_id: int) -> EnhancedDOMTreeNode:
	"""Create a minimal interactive node used by click/type events in tests."""
	return EnhancedDOMTreeNode(
		node_id=backend_node_id,
		backend_node_id=backend_node_id,
		node_type=NodeType.ELEMENT_NODE,
		node_name='INPUT',
		node_value='',
		attributes={'type': 'text'},
		is_scrollable=False,
		is_visible=True,
		absolute_position=DOMRect(x=10, y=20, width=100, height=40),
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
async def test_click_element_falls_back_to_cached_rect_when_relocate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Click should use cached absolute_position when selector-based lookup fails."""
	session = SafariBrowserSession()
	node = _make_node(1)
	click_calls: list[tuple[float, float]] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del operation_name, retries
		return await operation()

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del wait_for_new, timeout_seconds
		return []

	async def fake_resolve_element_ref(event_node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del event_node
		return safari_session_module._SafariElementRef(
			backend_node_id=1,
			stable_id='stable-1',
			tag_name='input',
			text_content='',
			attributes={'type': 'text'},
			css_selector='input',
			xpath='//input[1]',
			absolute_position=DOMRect(x=10, y=20, width=100, height=40),
		)

	async def fake_scroll_element_into_view(ref: Any) -> dict[str, Any]:
		del ref
		return {'ok': False, 'error': 'not found'}

	async def fake_click_at(x: float, y: float) -> None:
		click_calls.append((x, y))

	async def fake_dismiss_dialog_if_any() -> None:
		return None

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session, '_scroll_element_into_view', fake_scroll_element_into_view)
	monkeypatch.setattr(session.driver, 'click_at', fake_click_at)
	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)

	try:
		result = await session.on_ClickElementEvent(ClickElementEvent(node=node))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert click_calls == [(60.0, 40.0)]
	assert result == {'x': 60, 'y': 40}
	assert 'fallback:cached-rect-click:1' in session._recent_events


@pytest.mark.asyncio
async def test_type_text_falls_back_to_cached_rect_when_relocate_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Type should use cached absolute_position when selector-based focus fails."""
	session = SafariBrowserSession()
	node = _make_node(7)
	click_calls: list[tuple[float, float]] = []
	typed_values: list[str] = []
	execute_js_calls = 0

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del operation_name, retries
		return await operation()

	async def fake_resolve_element_ref(event_node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del event_node
		return safari_session_module._SafariElementRef(
			backend_node_id=7,
			stable_id='stable-7',
			tag_name='input',
			text_content='',
			attributes={'type': 'text'},
			css_selector='input',
			xpath='//input[1]',
			absolute_position=DOMRect(x=100, y=150, width=80, height=20),
		)

	async def fake_scroll_element_into_view(ref: Any) -> dict[str, Any]:
		del ref
		return {'ok': False, 'error': 'not found'}

	async def fake_click_at(x: float, y: float) -> None:
		click_calls.append((x, y))

	async def fake_send_keys(text: str) -> None:
		typed_values.append(text)

	async def fake_execute_js(script: str, *args: Any) -> Any:
		nonlocal execute_js_calls
		del script, args
		execute_js_calls += 1
		if execute_js_calls == 1:
			return True
		return 'hello'

	async def fake_dismiss_dialog_if_any() -> None:
		return None

	async def fake_refresh_tabs() -> list[TabInfo]:
		return [TabInfo(url='https://example.com', title='Example', target_id='safari-target', parent_target_id=None)]

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session, '_scroll_element_into_view', fake_scroll_element_into_view)
	monkeypatch.setattr(session.driver, 'click_at', fake_click_at)
	monkeypatch.setattr(session.driver, 'send_keys', fake_send_keys)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)
	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)
	monkeypatch.setattr(session, '_refresh_tabs', fake_refresh_tabs)

	try:
		result = await session.on_TypeTextEvent(TypeTextEvent(node=node, text='hello', clear=True))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert click_calls == [(140.0, 160.0)]
	assert typed_values == ['hello']
	assert result == {'x': 140, 'y': 160, 'actual_value': 'hello'}
	assert 'fallback:cached-rect-type:7' in session._recent_events
