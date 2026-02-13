"""Async Safari WebDriver wrapper used by SafariBrowserSession.

This module intentionally keeps Selenium calls behind ``asyncio.to_thread()`` so
Browser Use's async event bus is never blocked by synchronous WebDriver calls.
"""

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, validate_call

T = TypeVar('T')

# Key aliases for SendKeysEvent support.
_SELENIUM_KEY_ALIASES: dict[str, str] = {
	'backspace': 'BACKSPACE',
	'tab': 'TAB',
	'enter': 'ENTER',
	'return': 'RETURN',
	'esc': 'ESCAPE',
	'escape': 'ESCAPE',
	'space': 'SPACE',
	'pageup': 'PAGE_UP',
	'pagedown': 'PAGE_DOWN',
	'end': 'END',
	'home': 'HOME',
	'left': 'ARROW_LEFT',
	'arrowleft': 'ARROW_LEFT',
	'up': 'ARROW_UP',
	'arrowup': 'ARROW_UP',
	'right': 'ARROW_RIGHT',
	'arrowright': 'ARROW_RIGHT',
	'down': 'ARROW_DOWN',
	'arrowdown': 'ARROW_DOWN',
	'insert': 'INSERT',
	'delete': 'DELETE',
	'cmd': 'COMMAND',
	'command': 'COMMAND',
	'meta': 'COMMAND',
	'ctrl': 'CONTROL',
	'control': 'CONTROL',
	'alt': 'ALT',
	'option': 'ALT',
	'shift': 'SHIFT',
	'f1': 'F1',
	'f2': 'F2',
	'f3': 'F3',
	'f4': 'F4',
	'f5': 'F5',
	'f6': 'F6',
	'f7': 'F7',
	'f8': 'F8',
	'f9': 'F9',
	'f10': 'F10',
	'f11': 'F11',
	'f12': 'F12',
}


class SafariDriverConfig(BaseModel):
	"""Configuration for the Safari WebDriver wrapper."""

	model_config = ConfigDict(extra='forbid')

	executable_path: str = '/usr/bin/safaridriver'
	command_timeout: float = Field(default=45.0, gt=0)
	page_load_timeout: float = Field(default=60.0, gt=0)
	script_timeout: float = Field(default=30.0, gt=0)
	implicit_wait_timeout: float = Field(default=0.0, ge=0)


class SafariTabInfo(BaseModel):
	"""Tab metadata from Safari's WebDriver window handles."""

	model_config = ConfigDict(extra='forbid')

	index: int
	handle: str
	url: str
	title: str


class SafariDriverNotStartedError(RuntimeError):
	"""Raised when attempting to use SafariDriver before start()."""


class SafariDriver:
	"""Thin async wrapper around Selenium Safari WebDriver."""

	def __init__(self, config: SafariDriverConfig | None = None):
		self.config = config or SafariDriverConfig()
		self._driver: Any | None = None
		self._lock = asyncio.Lock()

	@property
	def is_started(self) -> bool:
		return self._driver is not None

	def _require_driver(self) -> Any:
		if self._driver is None:
			raise SafariDriverNotStartedError('SafariDriver is not started. Call await start() first.')
		return self._driver

	async def _run_sync(self, operation: Callable[[], T], timeout: float | None = None) -> T:
		"""Run a blocking Selenium operation in a thread with timeout."""
		return await asyncio.wait_for(asyncio.to_thread(operation), timeout=timeout or self.config.command_timeout)

	async def _with_driver(self, operation: Callable[[Any], T], timeout: float | None = None) -> T:
		"""Run a blocking operation while holding the driver lock."""
		async with self._lock:
			driver = self._require_driver()
			return await self._run_sync(lambda: operation(driver), timeout=timeout)

	def _resolve_locator(self, by: str) -> str:
		from selenium.webdriver.common.by import By

		if by == 'css':
			return By.CSS_SELECTOR
		if by == 'xpath':
			return By.XPATH
		raise ValueError(f'Unsupported locator type: {by}')

	def _parse_send_keys_token(self, token: str) -> Any:
		from selenium.webdriver.common.keys import Keys

		cleaned = token.strip()
		if not cleaned:
			return ''
		if len(cleaned) == 1:
			return cleaned
		mapped = _SELENIUM_KEY_ALIASES.get(cleaned.lower())
		if mapped is None:
			return cleaned
		return getattr(Keys, mapped)

	@validate_call
	async def start(self) -> None:
		"""Start a Safari WebDriver session if one is not already running."""
		async with self._lock:
			if self._driver is not None:
				return

			def _start_sync() -> Any:
				try:
					from selenium import webdriver
					from selenium.common.exceptions import SessionNotCreatedException
					from selenium.webdriver.safari.options import Options as SafariOptions
					from selenium.webdriver.safari.service import Service as SafariService
				except ImportError as exc:
					raise RuntimeError(
						'Selenium is required for Safari support. Install with `uv add selenium`. '
						'Also run `/usr/bin/safaridriver --enable` once on macOS.'
					) from exc

				service = SafariService(executable_path=self.config.executable_path)
				options = SafariOptions()
				try:
					driver = webdriver.Safari(service=service, options=options)
				except SessionNotCreatedException as exc:
					message = str(exc)
					if 'Allow remote automation' in message:
						raise RuntimeError(
							'Safari WebDriver is disabled. Enable it in Safari Settings -> Advanced -> '
							'Show features for web developers, then in the Develop menu enable '
							'"Allow Remote Automation".'
						) from exc
					raise RuntimeError(f'Failed to start Safari WebDriver session: {message}') from exc
				driver.set_page_load_timeout(self.config.page_load_timeout)
				driver.set_script_timeout(self.config.script_timeout)
				if self.config.implicit_wait_timeout > 0:
					driver.implicitly_wait(self.config.implicit_wait_timeout)
				return driver

			self._driver = await self._run_sync(_start_sync)

	@validate_call
	async def close(self) -> None:
		"""Close the current Safari WebDriver session."""
		async with self._lock:
			if self._driver is None:
				return
			driver = self._driver
			self._driver = None

		await self._run_sync(driver.quit)

	async def __aenter__(self) -> 'SafariDriver':
		await self.start()
		return self

	async def __aexit__(self, exc_type, exc, tb) -> None:
		await self.close()

	async def is_alive(self) -> bool:
		"""Check whether the driver session is still responsive."""
		if self._driver is None:
			return False
		try:
			await self._with_driver(lambda d: d.title)
			return True
		except Exception:
			return False

	@validate_call
	async def navigate(self, url: str) -> str:
		"""Navigate to a URL and return the final URL."""
		return await self._with_driver(lambda d: (d.get(url), d.current_url)[1], timeout=self.config.page_load_timeout)

	async def screenshot(self) -> str:
		"""Capture a screenshot as base64 string."""
		return await self._with_driver(lambda d: d.get_screenshot_as_base64())

	@validate_call
	async def execute_js(self, expression: str, *args: Any) -> Any:
		"""Execute JavaScript in the current tab."""
		return await self._with_driver(lambda d: d.execute_script(expression, *args), timeout=self.config.script_timeout)

	@validate_call
	async def execute_async_js(self, expression: str, *args: Any) -> Any:
		"""Execute asynchronous JavaScript and wait for callback result."""
		return await self._with_driver(lambda d: d.execute_async_script(expression, *args), timeout=self.config.script_timeout)

	@validate_call
	async def click_at(self, x: float, y: float) -> None:
		"""Click viewport coordinates using ActionChains with JS fallback."""

		def _click_sync(driver: Any) -> None:
			from selenium.webdriver import ActionChains
			from selenium.webdriver.common.by import By

			try:
				body = driver.find_element(By.TAG_NAME, 'body')
				ActionChains(driver).move_to_element_with_offset(body, int(x), int(y)).click().perform()
				return
			except Exception:
				clicked = driver.execute_script(
					"""
					const target = document.elementFromPoint(arguments[0], arguments[1]);
					if (!target) return false;
					target.click();
					return true;
					""",
					x,
					y,
				)
				if not clicked:
					raise RuntimeError(f'Unable to click point ({x}, {y})')

		await self._with_driver(_click_sync)

	@validate_call
	async def click_selector(self, selector: str, by: str = 'css') -> None:
		"""Click an element using a CSS or XPath selector."""

		def _click_sync(driver: Any) -> None:
			locator = self._resolve_locator(by)
			element = driver.find_element(locator, selector)
			element.click()

		await self._with_driver(_click_sync)

	@validate_call
	async def type_into(self, selector: str, text: str, clear: bool = True, by: str = 'css') -> None:
		"""Type into an element located by CSS selector or XPath."""

		def _type_sync(driver: Any) -> None:
			locator = self._resolve_locator(by)
			element = driver.find_element(locator, selector)
			if clear:
				element.clear()
			element.send_keys(text)

		await self._with_driver(_type_sync)

	@validate_call
	async def upload_file(self, selector: str, file_path: str, by: str = 'css') -> None:
		"""Upload a local file through a file input element."""

		def _upload_sync(driver: Any) -> None:
			locator = self._resolve_locator(by)
			element = driver.find_element(locator, selector)
			element.send_keys(file_path)

		await self._with_driver(_upload_sync)

	@validate_call
	async def send_keys(self, keys: str) -> None:
		"""Send a key sequence to the currently focused element."""

		def _send_sync(driver: Any) -> None:
			from selenium.webdriver import ActionChains

			chain = ActionChains(driver)
			for token in keys.split():
				if '+' in token:
					parts = [part for part in token.split('+') if part]
					if len(parts) <= 1:
						chain.send_keys(self._parse_send_keys_token(token))
						continue
					mods = [self._parse_send_keys_token(part) for part in parts[:-1]]
					main_key = self._parse_send_keys_token(parts[-1])
					for mod in mods:
						chain.key_down(mod)
					chain.send_keys(main_key)
					for mod in reversed(mods):
						chain.key_up(mod)
				else:
					chain.send_keys(self._parse_send_keys_token(token))
			chain.perform()

		await self._with_driver(_send_sync)

	@validate_call
	async def scroll(self, direction: str, pixels: int) -> None:
		"""Scroll page up/down/left/right using JavaScript."""
		dx, dy = 0, 0
		direction_lower = direction.lower()
		if direction_lower == 'down':
			dy = abs(pixels)
		elif direction_lower == 'up':
			dy = -abs(pixels)
		elif direction_lower == 'right':
			dx = abs(pixels)
		elif direction_lower == 'left':
			dx = -abs(pixels)
		else:
			raise ValueError(f'Unsupported scroll direction: {direction}')
		await self.execute_js('window.scrollBy(arguments[0], arguments[1]);', dx, dy)

	async def go_back(self) -> None:
		await self._with_driver(lambda d: d.back())

	async def go_forward(self) -> None:
		await self._with_driver(lambda d: d.forward())

	async def refresh(self) -> None:
		await self._with_driver(lambda d: d.refresh())

	async def get_url(self) -> str:
		return await self._with_driver(lambda d: d.current_url)

	async def get_title(self) -> str:
		return await self._with_driver(lambda d: d.title)

	async def get_window_handle(self) -> str:
		return await self._with_driver(lambda d: d.current_window_handle)

	async def get_cookies(self) -> list[dict[str, Any]]:
		return await self._with_driver(lambda d: d.get_cookies())

	@validate_call
	async def set_cookie(self, cookie: dict[str, Any]) -> None:
		await self._with_driver(lambda d: d.add_cookie(cookie))

	async def clear_cookies(self) -> None:
		await self._with_driver(lambda d: d.delete_all_cookies())

	async def list_tabs(self) -> list[SafariTabInfo]:
		"""List tabs (window handles) with title and URL."""

		def _list_sync(driver: Any) -> list[SafariTabInfo]:
			from selenium.common.exceptions import WebDriverException

			current = driver.current_window_handle
			tabs: list[SafariTabInfo] = []
			for index, handle in enumerate(driver.window_handles):
				driver.switch_to.window(handle)
				try:
					title = driver.title
					url = driver.current_url
				except WebDriverException:
					title = ''
					url = 'about:blank'
				tabs.append(SafariTabInfo(index=index, handle=handle, title=title, url=url))
			driver.switch_to.window(current)
			return tabs

		return await self._with_driver(_list_sync)

	@validate_call
	async def switch_tab(self, index: int) -> SafariTabInfo:
		"""Switch to tab by index and return tab metadata."""

		def _switch_sync(driver: Any) -> SafariTabInfo:
			handles = driver.window_handles
			if index < 0 or index >= len(handles):
				raise IndexError(f'Tab index {index} out of range: 0..{len(handles) - 1}')
			handle = handles[index]
			driver.switch_to.window(handle)
			return SafariTabInfo(index=index, handle=handle, title=driver.title, url=driver.current_url)

		return await self._with_driver(_switch_sync)

	@validate_call
	async def switch_to_handle(self, handle: str) -> SafariTabInfo:
		"""Switch to tab by handle and return tab metadata."""

		def _switch_sync(driver: Any) -> SafariTabInfo:
			handles = driver.window_handles
			if handle not in handles:
				raise IndexError(f'Tab handle not found: {handle}')
			driver.switch_to.window(handle)
			index = handles.index(handle)
			return SafariTabInfo(index=index, handle=handle, title=driver.title, url=driver.current_url)

		return await self._with_driver(_switch_sync)

	@validate_call
	async def new_tab(self, url: str = 'about:blank') -> SafariTabInfo:
		"""Open a new tab and navigate to URL."""

		def _new_tab_sync(driver: Any) -> SafariTabInfo:
			driver.switch_to.new_window('tab')
			driver.get(url)
			handle = driver.current_window_handle
			index = driver.window_handles.index(handle)
			return SafariTabInfo(index=index, handle=handle, title=driver.title, url=driver.current_url)

		return await self._with_driver(_new_tab_sync, timeout=self.config.page_load_timeout)

	@validate_call
	async def close_tab(self, index: int | None = None) -> SafariTabInfo | None:
		"""Close current tab or tab by index and switch to a remaining tab."""

		def _close_sync(driver: Any) -> SafariTabInfo | None:
			handles = driver.window_handles
			if not handles:
				return None

			if index is not None:
				if index < 0 or index >= len(handles):
					raise IndexError(f'Tab index {index} out of range: 0..{len(handles) - 1}')
				driver.switch_to.window(handles[index])

			driver.close()
			remaining = driver.window_handles
			if not remaining:
				return None

			driver.switch_to.window(remaining[-1])
			active = driver.current_window_handle
			active_index = remaining.index(active)
			return SafariTabInfo(index=active_index, handle=active, title=driver.title, url=driver.current_url)

		return await self._with_driver(_close_sync)

	@validate_call
	async def handle_dialog(self, accept: bool = True, text: str | None = None) -> bool:
		"""Accept or dismiss the current JavaScript dialog if present."""

		def _dialog_sync(driver: Any) -> bool:
			from selenium.common.exceptions import NoAlertPresentException

			try:
				alert = driver.switch_to.alert
			except NoAlertPresentException:
				return False

			if text:
				try:
					alert.send_keys(text)
				except Exception:
					pass
			if accept:
				alert.accept()
			else:
				alert.dismiss()
			return True

		return await self._with_driver(_dialog_sync)

	@validate_call
	async def wait_for_ready_state(self, timeout_seconds: float = 15.0) -> str:
		"""Poll ``document.readyState`` until complete or timeout."""
		end_time = asyncio.get_running_loop().time() + timeout_seconds
		last_state = 'loading'
		while asyncio.get_running_loop().time() < end_time:
			state = await self.execute_js('return document.readyState;')
			last_state = str(state)
			if last_state == 'complete':
				return last_state
			await asyncio.sleep(0.1)
		return last_state
