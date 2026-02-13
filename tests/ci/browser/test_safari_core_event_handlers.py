"""Coverage for Safari core event handlers not exercised by fallback-specific tests."""

from pathlib import Path
from typing import Any

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.events import (
	GetDropdownOptionsEvent,
	GoBackEvent,
	GoForwardEvent,
	RefreshEvent,
	ScrollEvent,
	ScrollToTextEvent,
	SelectDropdownOptionEvent,
	SendKeysEvent,
	UploadFileEvent,
)
from browser_use.browser.views import BrowserError
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType
from safari_session import SafariBrowserSession


def _make_node(backend_node_id: int = 1) -> EnhancedDOMTreeNode:
	return EnhancedDOMTreeNode(
		node_id=backend_node_id,
		backend_node_id=backend_node_id,
		node_type=NodeType.ELEMENT_NODE,
		node_name='SELECT',
		node_value='',
		attributes={'id': f'node-{backend_node_id}'},
		is_scrollable=False,
		is_visible=True,
		absolute_position=DOMRect(x=20, y=30, width=180, height=40),
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


def _make_ref(
	*,
	backend_node_id: int = 1,
	css_selector: str | None = '#upload',
	xpath: str | None = '//*[@id="upload"]',
) -> safari_session_module._SafariElementRef:
	return safari_session_module._SafariElementRef(
		backend_node_id=backend_node_id,
		stable_id=f'stable-{backend_node_id}',
		tag_name='input',
		text_content='',
		attributes={},
		css_selector=css_selector,
		xpath=xpath,
		absolute_position=DOMRect(x=10, y=20, width=120, height=24),
	)


@pytest.mark.asyncio
async def test_scroll_event_scrolls_page_and_records_recent_event(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	calls: list[tuple[Any, ...]] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		assert operation_name == 'ScrollEvent'
		del retries
		return await operation()

	async def fake_execute_js(script: str, *args: Any) -> Any:
		calls.append((script, *args))
		return True

	async def fake_dismiss_dialog_if_any() -> None:
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)
	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)

	try:
		await session.on_ScrollEvent(ScrollEvent(direction='down', amount=180, node=None))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert calls
	assert 'window.scrollBy(0, arguments[0]);' in calls[0][0]
	assert calls[0][1] == 180
	assert 'scroll:down:180' in session._recent_events


@pytest.mark.asyncio
async def test_scroll_to_text_raises_browser_error_when_text_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		assert operation_name == 'ScrollToTextEvent'
		del retries
		return await operation()

	async def fake_execute_js(script: str, text: str) -> bool:
		del script, text
		return False

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		with pytest.raises(BrowserError, match="Text 'missing phrase' not found on page"):
			await session.on_ScrollToTextEvent(ScrollToTextEvent(text='missing phrase'))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
	('event_name', 'event_cls', 'driver_method', 'expected_recent'),
	[
		('GoBackEvent', GoBackEvent, 'go_back', 'go_back'),
		('GoForwardEvent', GoForwardEvent, 'go_forward', 'go_forward'),
		('RefreshEvent', RefreshEvent, 'refresh', 'refresh'),
	],
)
async def test_history_navigation_events_use_retry_and_record_recent_event(
	monkeypatch: pytest.MonkeyPatch,
	event_name: str,
	event_cls: type[GoBackEvent | GoForwardEvent | RefreshEvent],
	driver_method: str,
	expected_recent: str,
) -> None:
	session = SafariBrowserSession()
	retry_names: list[str] = []
	driver_calls: list[str] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del retries
		retry_names.append(operation_name)
		return await operation()

	async def fake_driver_method() -> None:
		driver_calls.append(driver_method)

	async def fake_wait_for_ready_state(timeout_seconds: float = 8.0) -> str:
		assert timeout_seconds == 8.0
		return 'complete'

	async def fake_dismiss_dialog_if_any() -> None:
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session.driver, driver_method, fake_driver_method)
	monkeypatch.setattr(session.driver, 'wait_for_ready_state', fake_wait_for_ready_state)
	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)

	try:
		handler = getattr(session, f'on_{event_name}')
		await handler(event_cls())
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert retry_names == [event_name]
	assert driver_calls == [driver_method]
	assert expected_recent in session._recent_events


@pytest.mark.asyncio
async def test_send_keys_event_uses_retry_and_invalidates_cache(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	retry_names: list[str] = []
	sent_keys: list[str] = []
	session._cached_selector_map = {1: _make_node(1)}

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del retries
		retry_names.append(operation_name)
		return await operation()

	async def fake_send_keys(keys: str) -> None:
		sent_keys.append(keys)

	async def fake_dismiss_dialog_if_any() -> None:
		return None

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session.driver, 'send_keys', fake_send_keys)
	monkeypatch.setattr(session, '_dismiss_dialog_if_any', fake_dismiss_dialog_if_any)

	try:
		await session.on_SendKeysEvent(SendKeysEvent(keys='Tab Enter'))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert retry_names == ['SendKeysEvent']
	assert sent_keys == ['Tab Enter']
	assert not session._cached_selector_map
	assert 'send_keys:Tab Enter' in session._recent_events


@pytest.mark.asyncio
async def test_upload_file_event_uses_css_selector_when_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	session = SafariBrowserSession()
	retry_names: list[str] = []
	upload_calls: list[tuple[str, str, str]] = []
	file_path = str(tmp_path / 'sample.txt')

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del retries
		retry_names.append(operation_name)
		return await operation()

	async def fake_resolve_element_ref(node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del node
		return _make_ref(css_selector='#upload', xpath='//*[@id="upload"]')

	async def fake_upload_file(selector: str, upload_path: str, by: str = 'css') -> None:
		upload_calls.append((selector, upload_path, by))

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session.driver, 'upload_file', fake_upload_file)

	try:
		await session.on_UploadFileEvent(UploadFileEvent(node=_make_node(11), file_path=file_path))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert retry_names == ['UploadFileEvent']
	assert upload_calls == [('#upload', file_path, 'css')]
	assert f'upload:{Path(file_path).name}' in session._recent_events


@pytest.mark.asyncio
async def test_upload_file_event_falls_back_to_xpath_when_css_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	session = SafariBrowserSession()
	upload_calls: list[tuple[str, str, str]] = []
	file_path = str(tmp_path / 'photo.png')

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		assert operation_name == 'UploadFileEvent'
		del retries
		return await operation()

	async def fake_resolve_element_ref(node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del node
		return _make_ref(css_selector=None, xpath='//input[@type="file"]')

	async def fake_upload_file(selector: str, upload_path: str, by: str = 'css') -> None:
		upload_calls.append((selector, upload_path, by))

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session.driver, 'upload_file', fake_upload_file)

	try:
		await session.on_UploadFileEvent(UploadFileEvent(node=_make_node(12), file_path=file_path))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert upload_calls == [('//input[@type="file"]', file_path, 'xpath')]


@pytest.mark.asyncio
async def test_upload_file_event_raises_when_element_has_no_selectors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
	session = SafariBrowserSession()
	file_path = str(tmp_path / 'data.csv')

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		assert operation_name == 'UploadFileEvent'
		del retries
		return await operation()

	async def fake_resolve_element_ref(node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del node
		return _make_ref(css_selector=None, xpath=None)

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)

	try:
		with pytest.raises(BrowserError, match='has no selector for upload'):
			await session.on_UploadFileEvent(UploadFileEvent(node=_make_node(13), file_path=file_path))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_get_dropdown_options_formats_output_and_records_event(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()
	retry_names: list[str] = []

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del retries
		retry_names.append(operation_name)
		return await operation()

	async def fake_resolve_element_ref(node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del node
		return _make_ref(css_selector='#country', xpath='//*[@id="country"]')

	async def fake_execute_js(script: str, *args: Any) -> dict[str, Any]:
		del script, args
		return {
			'type': 'select',
			'options': [
				{'index': 0, 'text': 'USA', 'value': 'us', 'selected': True},
				{'index': 1, 'text': 'Japan', 'value': 'jp', 'selected': False},
			],
		}

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		result = await session.on_GetDropdownOptionsEvent(GetDropdownOptionsEvent(node=_make_node(21)))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert retry_names == ['GetDropdownOptionsEvent']
	assert result['type'] == 'select'
	assert 'formatted_options' in result
	assert 'USA' in result['formatted_options']
	assert 'Japan' in result['formatted_options']
	assert 'dropdown_options:21' in session._recent_events


@pytest.mark.asyncio
async def test_get_dropdown_options_returns_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		assert operation_name == 'GetDropdownOptionsEvent'
		del retries
		return await operation()

	async def fake_resolve_element_ref(node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del node
		return _make_ref(css_selector='#missing', xpath='//*[@id="missing"]')

	async def fake_execute_js(script: str, *args: Any) -> dict[str, str]:
		del script, args
		return {'error': 'Dropdown element not found'}

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		result = await session.on_GetDropdownOptionsEvent(GetDropdownOptionsEvent(node=_make_node(22)))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert result['error'] == 'Dropdown element not found'
	assert result['short_term_memory'] == 'Dropdown element not found'


@pytest.mark.asyncio
@pytest.mark.parametrize(
	('js_result', 'expected_success', 'expected_fragment'),
	[
		({'success': True, 'message': 'Selected option: USA'}, 'true', 'Selected option: USA'),
		({'success': False, 'error': "Option 'Mars' not found"}, 'false', "Option 'Mars' not found"),
	],
)
async def test_select_dropdown_option_returns_structured_result(
	monkeypatch: pytest.MonkeyPatch,
	js_result: dict[str, Any],
	expected_success: str,
	expected_fragment: str,
) -> None:
	session = SafariBrowserSession()
	retry_names: list[str] = []
	session._cached_selector_map = {21: _make_node(21)}

	async def fake_start(self: SafariBrowserSession) -> None:
		del self

	async def fake_with_retry(operation_name: str, operation, retries: int = 2):  # type: ignore[no-untyped-def]
		del retries
		retry_names.append(operation_name)
		return await operation()

	async def fake_resolve_element_ref(node: EnhancedDOMTreeNode):  # type: ignore[no-untyped-def]
		del node
		return _make_ref(css_selector='#country', xpath='//*[@id="country"]')

	async def fake_execute_js(script: str, *args: Any) -> dict[str, Any]:
		del script, args
		return js_result

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(session, '_with_retry', fake_with_retry)
	monkeypatch.setattr(session, '_resolve_element_ref', fake_resolve_element_ref)
	monkeypatch.setattr(session.driver, 'execute_js', fake_execute_js)

	try:
		result = await session.on_SelectDropdownOptionEvent(SelectDropdownOptionEvent(node=_make_node(21), text='USA'))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert retry_names == ['SelectDropdownOptionEvent']
	assert result['success'] == expected_success
	assert any(expected_fragment in value for value in result.values())
	assert not session._cached_selector_map
	assert any(event.startswith('dropdown_select:21:') for event in session._recent_events)
