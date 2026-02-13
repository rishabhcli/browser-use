"""Unit tests for SafariDriver's async Selenium wrapper behavior."""

from __future__ import annotations

import builtins
import sys
import types
from typing import Any

import pytest
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from safari_session.driver import SafariDriver, SafariDriverConfig, SafariDriverNotStartedError, SafariTabInfo


class _FakeAlert:
	def __init__(self) -> None:
		self.sent_text: str | None = None
		self.accepted = False
		self.dismissed = False

	def send_keys(self, value: str) -> None:
		self.sent_text = value

	def accept(self) -> None:
		self.accepted = True

	def dismiss(self) -> None:
		self.dismissed = True


class _FakeSwitchTo:
	def __init__(self, driver: _FakeWebDriver) -> None:
		self._driver = driver

	def window(self, handle: str) -> None:
		if handle not in self._driver.window_handles:
			raise IndexError(f'Unknown handle {handle}')
		self._driver.active_handle = handle

	def new_window(self, kind: str) -> None:
		if kind != 'tab':
			raise ValueError(f'Unsupported new window kind: {kind}')
		new_handle = f'h{len(self._driver.window_handles) + 1}'
		self._driver.window_handles.append(new_handle)
		self._driver.tab_data[new_handle] = {'title': '', 'url': 'about:blank'}
		self._driver.active_handle = new_handle

	@property
	def alert(self) -> _FakeAlert:
		if self._driver.active_alert is None:
			raise NoAlertPresentException()
		return self._driver.active_alert


class _FakeWebDriver:
	def __init__(self) -> None:
		self.window_handles = ['h1', 'h2']
		self.active_handle = 'h1'
		self.tab_data: dict[str, dict[str, str]] = {
			'h1': {'title': 'One', 'url': 'https://one.example'},
			'h2': {'title': 'Two', 'url': 'https://two.example'},
		}
		self.cookies: list[dict[str, Any]] = [{'name': 'a', 'value': '1'}]
		self.active_alert: _FakeAlert | None = None
		self.quit_called = False
		self.switch_to = _FakeSwitchTo(self)

	@property
	def current_window_handle(self) -> str:
		return self.active_handle

	@property
	def title(self) -> str:
		return self.tab_data[self.active_handle]['title']

	@property
	def current_url(self) -> str:
		return self.tab_data[self.active_handle]['url']

	def get(self, url: str) -> None:
		self.tab_data[self.active_handle]['url'] = url
		self.tab_data[self.active_handle]['title'] = f'Title for {url}'

	def get_screenshot_as_base64(self) -> str:
		return 'base64-screenshot'

	def execute_script(self, expression: str, *args: Any) -> Any:
		return {'expression': expression, 'args': args}

	def execute_async_script(self, expression: str, *args: Any) -> Any:
		return {'expression': expression, 'args': args, 'async': True}

	def get_cookies(self) -> list[dict[str, Any]]:
		return list(self.cookies)

	def add_cookie(self, cookie: dict[str, Any]) -> None:
		self.cookies.append(cookie)

	def delete_all_cookies(self) -> None:
		self.cookies.clear()

	def back(self) -> None:
		return None

	def forward(self) -> None:
		return None

	def refresh(self) -> None:
		return None

	def close(self) -> None:
		handle = self.active_handle
		if handle in self.window_handles:
			self.window_handles.remove(handle)
			self.tab_data.pop(handle, None)
		if self.window_handles:
			self.active_handle = self.window_handles[-1]

	def quit(self) -> None:
		self.quit_called = True


class _FakeElement:
	def __init__(self) -> None:
		self.clicked = False
		self.cleared = False
		self.sent_keys: list[str] = []

	def click(self) -> None:
		self.clicked = True

	def clear(self) -> None:
		self.cleared = True

	def send_keys(self, value: str) -> None:
		self.sent_keys.append(value)


@pytest.fixture
def started_driver(monkeypatch: pytest.MonkeyPatch) -> tuple[SafariDriver, _FakeWebDriver]:
	driver = SafariDriver()
	fake = _FakeWebDriver()
	driver._driver = fake

	async def fake_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation()

	monkeypatch.setattr(driver, '_run_sync', fake_run_sync)
	return driver, fake


@pytest.mark.asyncio
async def test_driver_requires_start_before_use() -> None:
	driver = SafariDriver()
	with pytest.raises(SafariDriverNotStartedError):
		await driver.get_url()


def test_parse_send_keys_token_maps_known_aliases_and_passthrough() -> None:
	driver = SafariDriver()
	assert driver._parse_send_keys_token('enter') == Keys.ENTER
	assert driver._parse_send_keys_token('cmd') == Keys.COMMAND
	assert driver._parse_send_keys_token('ArrowDown') == Keys.ARROW_DOWN
	assert driver._parse_send_keys_token('A') == 'A'
	assert driver._parse_send_keys_token('unknown_key') == 'unknown_key'


@pytest.mark.asyncio
async def test_start_is_idempotent_when_driver_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	run_sync_calls = 0
	fake_driver = object()

	async def fake_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del operation, timeout
		nonlocal run_sync_calls
		run_sync_calls += 1
		return fake_driver

	monkeypatch.setattr(driver, '_run_sync', fake_run_sync)
	await driver.start()
	assert driver.is_started is True
	assert run_sync_calls == 1

	await driver.start()
	assert run_sync_calls == 1


@pytest.mark.asyncio
async def test_start_raises_helpful_error_when_selenium_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()

	async def fake_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation()

	original_import = builtins.__import__

	def fake_import(name: str, globals=None, locals=None, fromlist=(), level: int = 0):  # type: ignore[no-untyped-def]
		if name == 'selenium' or name.startswith('selenium.'):
			raise ImportError('selenium not installed')
		return original_import(name, globals, locals, fromlist, level)

	monkeypatch.setattr(driver, '_run_sync', fake_run_sync)
	monkeypatch.setattr(builtins, '__import__', fake_import)

	with pytest.raises(RuntimeError, match='Selenium is required for Safari support'):
		await driver.start()


def _install_fake_selenium_modules(
	monkeypatch: pytest.MonkeyPatch,
	safari_factory: Any,
	session_exception_class: type[Exception],
) -> None:
	"""Install a minimal fake selenium module tree for SafariDriver.start tests."""
	selenium_module = types.ModuleType('selenium')
	webdriver_module = types.ModuleType('selenium.webdriver')
	common_module = types.ModuleType('selenium.common')
	common_exceptions_module = types.ModuleType('selenium.common.exceptions')
	safari_pkg = types.ModuleType('selenium.webdriver.safari')
	safari_options_module = types.ModuleType('selenium.webdriver.safari.options')
	safari_service_module = types.ModuleType('selenium.webdriver.safari.service')

	class _FakeOptions:
		pass

	class _FakeService:
		def __init__(self, executable_path: str) -> None:
			self.executable_path = executable_path

	class _FakeWebDriverNamespace:
		@staticmethod
		def Safari(service: Any, options: Any) -> Any:
			return safari_factory(service=service, options=options)

	setattr(selenium_module, 'webdriver', _FakeWebDriverNamespace)
	setattr(webdriver_module, 'Safari', _FakeWebDriverNamespace.Safari)
	setattr(common_exceptions_module, 'SessionNotCreatedException', session_exception_class)
	setattr(common_module, 'exceptions', common_exceptions_module)
	setattr(safari_options_module, 'Options', _FakeOptions)
	setattr(safari_service_module, 'Service', _FakeService)

	monkeypatch.setitem(sys.modules, 'selenium', selenium_module)
	monkeypatch.setitem(sys.modules, 'selenium.webdriver', webdriver_module)
	monkeypatch.setitem(sys.modules, 'selenium.common', common_module)
	monkeypatch.setitem(sys.modules, 'selenium.common.exceptions', common_exceptions_module)
	monkeypatch.setitem(sys.modules, 'selenium.webdriver.safari', safari_pkg)
	monkeypatch.setitem(sys.modules, 'selenium.webdriver.safari.options', safari_options_module)
	monkeypatch.setitem(sys.modules, 'selenium.webdriver.safari.service', safari_service_module)


@pytest.mark.asyncio
async def test_start_raises_helpful_error_when_remote_automation_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()

	class _FakeSessionNotCreatedException(Exception):
		pass

	def safari_factory(service: Any, options: Any) -> Any:
		del service, options
		raise _FakeSessionNotCreatedException('Allow remote automation is disabled')

	async def fake_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation()

	_install_fake_selenium_modules(monkeypatch, safari_factory, _FakeSessionNotCreatedException)
	monkeypatch.setattr(driver, '_run_sync', fake_run_sync)

	with pytest.raises(RuntimeError, match='Safari WebDriver is disabled'):
		await driver.start()


@pytest.mark.asyncio
async def test_start_raises_generic_error_for_other_session_creation_failures(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()

	class _FakeSessionNotCreatedException(Exception):
		pass

	def safari_factory(service: Any, options: Any) -> Any:
		del service, options
		raise _FakeSessionNotCreatedException('some other startup issue')

	async def fake_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation()

	_install_fake_selenium_modules(monkeypatch, safari_factory, _FakeSessionNotCreatedException)
	monkeypatch.setattr(driver, '_run_sync', fake_run_sync)

	with pytest.raises(RuntimeError, match='Failed to start Safari WebDriver session: some other startup issue'):
		await driver.start()


@pytest.mark.asyncio
async def test_start_configures_timeouts_and_implicit_wait_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
	config = SafariDriverConfig(page_load_timeout=33.0, script_timeout=21.0, implicit_wait_timeout=2.5)
	driver = SafariDriver(config=config)
	calls: list[tuple[str, float]] = []

	class _FakeStartedDriver:
		def set_page_load_timeout(self, value: float) -> None:
			calls.append(('page_load', value))

		def set_script_timeout(self, value: float) -> None:
			calls.append(('script', value))

		def implicitly_wait(self, value: float) -> None:
			calls.append(('implicit', value))

	def safari_factory(service: Any, options: Any) -> _FakeStartedDriver:
		del service, options
		return _FakeStartedDriver()

	class _FakeSessionNotCreatedException(Exception):
		pass

	async def fake_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation()

	_install_fake_selenium_modules(monkeypatch, safari_factory, _FakeSessionNotCreatedException)
	monkeypatch.setattr(driver, '_run_sync', fake_run_sync)

	await driver.start()
	assert driver.is_started is True
	assert calls == [('page_load', 33.0), ('script', 21.0), ('implicit', 2.5)]


@pytest.mark.asyncio
async def test_close_is_noop_when_not_started(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()

	async def fail_run_sync(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del operation, timeout
		raise AssertionError('close() should not call _run_sync when not started')

	monkeypatch.setattr(driver, '_run_sync', fail_run_sync)
	await driver.close()
	assert driver.is_started is False


@pytest.mark.asyncio
async def test_context_manager_calls_start_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
	start_calls = 0
	close_calls = 0

	async def fake_start(self: SafariDriver) -> None:
		nonlocal start_calls
		del self
		start_calls += 1

	async def fake_close(self: SafariDriver) -> None:
		nonlocal close_calls
		del self
		close_calls += 1

	monkeypatch.setattr(SafariDriver, 'start', fake_start)
	monkeypatch.setattr(SafariDriver, 'close', fake_close)

	async with SafariDriver():
		pass

	assert start_calls == 1
	assert close_calls == 1


def test_resolve_locator_supports_css_and_xpath() -> None:
	driver = SafariDriver()
	assert driver._resolve_locator('css') == By.CSS_SELECTOR
	assert driver._resolve_locator('xpath') == By.XPATH
	with pytest.raises(ValueError, match='Unsupported locator type'):
		driver._resolve_locator('id')


@pytest.mark.asyncio
@pytest.mark.parametrize(
	('direction', 'pixels', 'expected'),
	[
		('down', 120, (0, 120)),
		('up', 120, (0, -120)),
		('right', 80, (80, 0)),
		('left', 80, (-80, 0)),
	],
)
async def test_scroll_translates_direction_to_window_scroll_by(
	monkeypatch: pytest.MonkeyPatch,
	direction: str,
	pixels: int,
	expected: tuple[int, int],
) -> None:
	driver = SafariDriver()
	calls: list[tuple[str, tuple[Any, ...]]] = []

	async def fake_execute_js(expression: str, *args: Any) -> Any:
		calls.append((expression, args))
		return None

	monkeypatch.setattr(driver, 'execute_js', fake_execute_js)
	await driver.scroll(direction, pixels)
	assert calls == [('window.scrollBy(arguments[0], arguments[1]);', expected)]


@pytest.mark.asyncio
async def test_scroll_raises_on_unsupported_direction() -> None:
	driver = SafariDriver()
	with pytest.raises(ValueError, match='Unsupported scroll direction'):
		await driver.scroll('diagonal', 10)


@pytest.mark.asyncio
async def test_tab_lifecycle_methods(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, fake = started_driver

	tabs = await driver.list_tabs()
	assert tabs == [
		SafariTabInfo(index=0, handle='h1', title='One', url='https://one.example'),
		SafariTabInfo(index=1, handle='h2', title='Two', url='https://two.example'),
	]
	assert fake.current_window_handle == 'h1'

	active = await driver.switch_tab(1)
	assert active.index == 1
	assert active.handle == 'h2'
	assert fake.current_window_handle == 'h2'

	active_by_handle = await driver.switch_to_handle('h1')
	assert active_by_handle.index == 0
	assert active_by_handle.handle == 'h1'
	assert fake.current_window_handle == 'h1'

	new_tab = await driver.new_tab('https://three.example')
	assert new_tab.url == 'https://three.example'
	assert new_tab.handle in {'h3'}
	assert fake.current_window_handle == 'h3'

	after_close = await driver.close_tab(index=0)
	assert after_close is not None
	assert after_close.handle == fake.current_window_handle
	assert 'h1' not in fake.window_handles


@pytest.mark.asyncio
async def test_switch_tab_and_close_tab_validate_indices(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, _ = started_driver

	with pytest.raises(IndexError, match='out of range'):
		await driver.switch_tab(99)

	with pytest.raises(IndexError, match='out of range'):
		await driver.close_tab(index=99)

	with pytest.raises(IndexError, match='not found'):
		await driver.switch_to_handle('missing')


@pytest.mark.asyncio
async def test_dialog_handling_accepts_and_dismisses(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, fake = started_driver
	alert = _FakeAlert()
	fake.active_alert = alert

	accepted = await driver.handle_dialog(accept=True, text='hello')
	assert accepted is True
	assert alert.sent_text == 'hello'
	assert alert.accepted is True

	alert2 = _FakeAlert()
	fake.active_alert = alert2
	dismissed = await driver.handle_dialog(accept=False)
	assert dismissed is True
	assert alert2.dismissed is True


@pytest.mark.asyncio
async def test_dialog_handling_continues_when_send_keys_fails(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, fake = started_driver

	class _AlertThatFailsOnSend(_FakeAlert):
		def send_keys(self, value: str) -> None:
			del value
			raise RuntimeError('cannot type into alert')

	alert = _AlertThatFailsOnSend()
	fake.active_alert = alert

	accepted = await driver.handle_dialog(accept=True, text='hello')
	assert accepted is True
	assert alert.accepted is True


@pytest.mark.asyncio
async def test_dialog_handling_returns_false_when_no_alert(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, fake = started_driver
	fake.active_alert = None
	assert await driver.handle_dialog(accept=True) is False


@pytest.mark.asyncio
async def test_cookie_methods_round_trip(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, _ = started_driver
	initial = await driver.get_cookies()
	assert initial == [{'name': 'a', 'value': '1'}]

	await driver.set_cookie({'name': 'b', 'value': '2'})
	updated = await driver.get_cookies()
	assert {'name': 'b', 'value': '2'} in updated

	await driver.clear_cookies()
	assert await driver.get_cookies() == []


@pytest.mark.asyncio
async def test_wait_for_ready_state_returns_complete_when_page_finishes_loading(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	states = iter(['loading', 'interactive', 'complete'])
	calls = 0

	async def fake_execute_js(expression: str, *args: Any) -> str:
		nonlocal calls
		del expression, args
		calls += 1
		return next(states)

	monkeypatch.setattr(driver, 'execute_js', fake_execute_js)
	state = await driver.wait_for_ready_state(timeout_seconds=0.6)
	assert state == 'complete'
	assert calls == 3


@pytest.mark.asyncio
async def test_wait_for_ready_state_times_out_and_returns_last_observed_state(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	driver = SafariDriver()

	async def fake_execute_js(expression: str, *args: Any) -> str:
		del expression, args
		return 'interactive'

	monkeypatch.setattr(driver, 'execute_js', fake_execute_js)
	state = await driver.wait_for_ready_state(timeout_seconds=0.11)
	assert state == 'interactive'


@pytest.mark.asyncio
async def test_close_stops_driver_and_is_alive_reflects_health(
	started_driver: tuple[SafariDriver, _FakeWebDriver], monkeypatch: pytest.MonkeyPatch
) -> None:
	driver, fake = started_driver
	assert await driver.is_alive() is True

	await driver.close()
	assert fake.quit_called is True
	assert driver.is_started is False
	assert await driver.is_alive() is False

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del operation, timeout
		raise RuntimeError('session dead')

	driver._driver = fake
	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)
	assert await driver.is_alive() is False


@pytest.mark.asyncio
async def test_navigation_and_script_helpers(started_driver: tuple[SafariDriver, _FakeWebDriver]) -> None:
	driver, _ = started_driver
	final_url = await driver.navigate('https://new.example')
	assert final_url == 'https://new.example'
	assert await driver.get_url() == 'https://new.example'
	assert await driver.get_title() == 'Title for https://new.example'
	assert await driver.get_window_handle() in {'h1', 'h2'}
	assert await driver.screenshot() == 'base64-screenshot'

	eval_result = await driver.execute_js('return 1 + 1;')
	assert eval_result['expression'] == 'return 1 + 1;'
	assert eval_result['args'] == ()

	async_result = await driver.execute_async_js('return Promise.resolve(1);', 'arg')
	assert async_result['expression'] == 'return Promise.resolve(1);'
	assert async_result['args'] == ('arg',)
	assert async_result['async'] is True


@pytest.mark.asyncio
async def test_click_selector_finds_element_by_css_and_clicks(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	element = _FakeElement()
	find_calls: list[tuple[str, str]] = []

	class _Driver:
		def find_element(self, locator: str, selector: str) -> _FakeElement:
			find_calls.append((locator, selector))
			return element

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_Driver())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)
	await driver.click_selector('button.primary', by='css')

	assert find_calls == [(By.CSS_SELECTOR, 'button.primary')]
	assert element.clicked is True


@pytest.mark.asyncio
async def test_type_into_respects_clear_flag(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	element = _FakeElement()
	find_calls: list[tuple[str, str]] = []

	class _Driver:
		def find_element(self, locator: str, selector: str) -> _FakeElement:
			find_calls.append((locator, selector))
			return element

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_Driver())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)

	await driver.type_into('//input[@name="q"]', 'hello', clear=True, by='xpath')
	assert find_calls == [(By.XPATH, '//input[@name="q"]')]
	assert element.cleared is True
	assert element.sent_keys == ['hello']

	element2 = _FakeElement()
	find_calls.clear()

	class _DriverNoClear:
		def find_element(self, locator: str, selector: str) -> _FakeElement:
			find_calls.append((locator, selector))
			return element2

	async def fake_with_driver_no_clear(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_DriverNoClear())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver_no_clear)
	await driver.type_into('input[name="q"]', 'world', clear=False, by='css')
	assert find_calls == [(By.CSS_SELECTOR, 'input[name="q"]')]
	assert element2.cleared is False
	assert element2.sent_keys == ['world']


@pytest.mark.asyncio
async def test_upload_file_sends_path_to_input(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	element = _FakeElement()
	find_calls: list[tuple[str, str]] = []

	class _Driver:
		def find_element(self, locator: str, selector: str) -> _FakeElement:
			find_calls.append((locator, selector))
			return element

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_Driver())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)
	await driver.upload_file('input[type="file"]', '/tmp/sample.txt', by='css')

	assert find_calls == [(By.CSS_SELECTOR, 'input[type="file"]')]
	assert element.sent_keys == ['/tmp/sample.txt']


@pytest.mark.asyncio
async def test_send_keys_builds_action_chain_with_chords(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	sequence: list[tuple[str, Any]] = []

	class _FakeActionChains:
		def __init__(self, web_driver: Any) -> None:
			del web_driver

		def key_down(self, key: Any) -> _FakeActionChains:
			sequence.append(('key_down', key))
			return self

		def send_keys(self, key: Any) -> _FakeActionChains:
			sequence.append(('send_keys', key))
			return self

		def key_up(self, key: Any) -> _FakeActionChains:
			sequence.append(('key_up', key))
			return self

		def perform(self) -> None:
			sequence.append(('perform', None))

	class _Driver:
		pass

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_Driver())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)
	monkeypatch.setattr('selenium.webdriver.ActionChains', _FakeActionChains)

	await driver.send_keys('cmd+a Enter')

	assert sequence
	assert sequence[-1] == ('perform', None)
	assert sequence[0][0] == 'key_down'
	assert sequence[1][0] == 'send_keys'
	assert sequence[2][0] == 'key_up'
	assert sequence[3][0] == 'send_keys'


@pytest.mark.asyncio
async def test_click_at_falls_back_to_js_when_action_chain_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()
	js_calls: list[tuple[str, tuple[Any, ...]]] = []

	class _FakeActionChains:
		def __init__(self, web_driver: Any) -> None:
			del web_driver

		def move_to_element_with_offset(self, element: Any, x: int, y: int) -> _FakeActionChains:
			del element, x, y
			raise RuntimeError('native click failed')

		def click(self) -> _FakeActionChains:
			return self

		def perform(self) -> None:
			return None

	class _Driver:
		def find_element(self, locator: str, value: str) -> str:
			assert locator == By.TAG_NAME
			assert value == 'body'
			return 'body'

		def execute_script(self, script: str, *args: Any) -> bool:
			js_calls.append((script, args))
			return True

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_Driver())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)
	monkeypatch.setattr('selenium.webdriver.ActionChains', _FakeActionChains)

	await driver.click_at(55.2, 44.7)
	assert js_calls
	assert js_calls[0][1] == (55.2, 44.7)


@pytest.mark.asyncio
async def test_click_at_raises_when_action_chain_and_js_fallback_fail(monkeypatch: pytest.MonkeyPatch) -> None:
	driver = SafariDriver()

	class _FakeActionChains:
		def __init__(self, web_driver: Any) -> None:
			del web_driver

		def move_to_element_with_offset(self, element: Any, x: int, y: int) -> _FakeActionChains:
			del element, x, y
			raise RuntimeError('native click failed')

		def click(self) -> _FakeActionChains:
			return self

		def perform(self) -> None:
			return None

	class _Driver:
		def find_element(self, locator: str, value: str) -> str:
			assert locator == By.TAG_NAME
			assert value == 'body'
			return 'body'

		def execute_script(self, script: str, *args: Any) -> bool:
			del script, args
			return False

	async def fake_with_driver(operation, timeout: float | None = None):  # type: ignore[no-untyped-def]
		del timeout
		return operation(_Driver())

	monkeypatch.setattr(driver, '_with_driver', fake_with_driver)
	monkeypatch.setattr('selenium.webdriver.ActionChains', _FakeActionChains)

	with pytest.raises(RuntimeError, match='Unable to click point'):
		await driver.click_at(10, 20)
