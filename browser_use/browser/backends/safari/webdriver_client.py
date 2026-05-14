"""Small async W3C WebDriver client for Safari and Safari Technology Preview."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

WEBDRIVER_ELEMENT_KEY = 'element-6066-11e4-a52e-4f735466cecf'
SAFARI_TP_DRIVER = Path('/Applications/Safari Technology Preview.app/Contents/MacOS/safaridriver')
SAFARI_DRIVER = Path('/usr/bin/safaridriver')


class WebDriverError(RuntimeError):
	"""Error returned by a WebDriver endpoint."""


class SafariDriverConfig(BaseModel):
	"""Configuration needed to start and connect to safaridriver."""

	model_config = ConfigDict(arbitrary_types_allowed=True)

	driver_path: Path
	browser_name: str
	port: int = 0
	env: dict[str, str | float | bool] | None = None
	start_timeout: float = 10.0
	request_timeout: float = 30.0
	keep_alive: bool = False
	diagnostic: bool = False

	@classmethod
	def from_channel(
		cls,
		channel: str | None,
		executable_path: str | Path | None = None,
		env: dict[str, str | float | bool] | None = None,
		keep_alive: bool = False,
	) -> 'SafariDriverConfig':
		"""Create a driver config from BrowserProfile channel/executable fields."""
		driver_path = Path(executable_path).expanduser() if executable_path else None
		channel_value = (channel or '').lower()

		if driver_path:
			name = driver_path.name.lower()
			if name.endswith('.app'):
				driver_path = driver_path / 'Contents' / 'MacOS' / 'safaridriver'
			browser_name = 'Safari Technology Preview' if 'technology' in str(driver_path).lower() else 'Safari'
			return cls(driver_path=driver_path, browser_name=browser_name, env=env, keep_alive=keep_alive)

		if channel_value in {'technology-preview', 'safari-technology-preview', 'safari-tp', 'stp'}:
			return cls(driver_path=SAFARI_TP_DRIVER, browser_name='Safari Technology Preview', env=env, keep_alive=keep_alive)

		return cls(driver_path=SAFARI_DRIVER, browser_name='Safari', env=env, keep_alive=keep_alive)


def find_free_port() -> int:
	"""Find a free localhost TCP port for safaridriver."""
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
		sock.bind(('127.0.0.1', 0))
		return int(sock.getsockname()[1])


class SafariWebDriverClient:
	"""Minimal async W3C WebDriver client for safaridriver."""

	def __init__(self, config: SafariDriverConfig) -> None:
		self.config = config
		self.port = config.port or find_free_port()
		self.base_url = f'http://127.0.0.1:{self.port}'
		self.session_id: str | None = None
		self.capabilities: dict[str, Any] = {}
		self._process: asyncio.subprocess.Process | None = None
		self._client: httpx.AsyncClient | None = None

	@property
	def process(self) -> asyncio.subprocess.Process | None:
		return self._process

	async def start_driver(self) -> None:
		"""Start safaridriver and wait until its HTTP status endpoint responds."""
		if not self.config.driver_path.exists():
			raise FileNotFoundError(f'safaridriver not found at {self.config.driver_path}')

		env = None
		if self.config.env:
			import os

			env = os.environ.copy()
			env.update({k: str(v) for k, v in self.config.env.items()})

		self._process = await asyncio.create_subprocess_exec(
			str(self.config.driver_path),
			'-p',
			str(self.port),
			stdout=asyncio.subprocess.PIPE,
			stderr=asyncio.subprocess.PIPE,
			env=env,
		)
		self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.config.request_timeout)

		deadline = asyncio.get_running_loop().time() + self.config.start_timeout
		last_error: BaseException | None = None
		while asyncio.get_running_loop().time() < deadline:
			if self._process.returncode is not None:
				stdout, stderr = await self._process.communicate()
				raise RuntimeError(
					f'safaridriver exited early with code {self._process.returncode}: '
					f'{stderr.decode(errors="replace") or stdout.decode(errors="replace")}'
				)
			try:
				await self.status()
				return
			except Exception as exc:
				last_error = exc
				await asyncio.sleep(0.1)

		raise TimeoutError(f'safaridriver did not become ready at {self.base_url}: {last_error}')

	async def create_session(self) -> dict[str, Any]:
		"""Create a Safari WebDriver session."""
		payload = {
			'capabilities': {
				'alwaysMatch': {
					'browserName': self.config.browser_name,
					'safari:automaticInspection': False,
					'safari:automaticProfiling': False,
				}
			}
		}
		value = await self._request('POST', '/session', json=payload)
		if isinstance(value, dict) and 'sessionId' in value:
			self.session_id = value['sessionId']
			self.capabilities = value.get('capabilities', {})
			return self.capabilities

		if isinstance(value, dict) and 'value' in value and isinstance(value['value'], dict):
			nested = value['value']
			self.session_id = nested.get('sessionId')
			self.capabilities = nested.get('capabilities', {})
			return self.capabilities

		raise WebDriverError(f'Unexpected session creation response: {value!r}')

	async def close(self, force: bool = False) -> None:
		"""Close the WebDriver session and safaridriver process."""
		if self.session_id:
			with contextlib.suppress(Exception):
				await self._request('DELETE', f'/session/{self.session_id}')
			self.session_id = None

		if self._client:
			await self._client.aclose()
			self._client = None

		if self._process and not self.config.keep_alive:
			if force:
				self._process.kill()
			else:
				self._process.terminate()
			with contextlib.suppress(Exception):
				await asyncio.wait_for(self._process.wait(), timeout=3)
			if self._process.returncode is None:
				self._process.kill()
				with contextlib.suppress(Exception):
					await asyncio.wait_for(self._process.wait(), timeout=3)
		self._process = None

	async def status(self) -> dict[str, Any]:
		value = await self._request('GET', '/status', session_required=False)
		return value if isinstance(value, dict) else {'value': value}

	async def navigate(self, url: str) -> None:
		await self._session_request('POST', '/url', json={'url': url})

	async def current_url(self) -> str:
		value = await self._session_request('GET', '/url')
		return str(value or '')

	async def title(self) -> str:
		value = await self._session_request('GET', '/title')
		return str(value or '')

	async def window_handles(self) -> list[str]:
		value = await self._session_request('GET', '/window/handles')
		return [str(handle) for handle in (value or [])]

	async def current_window_handle(self) -> str:
		value = await self._session_request('GET', '/window')
		return str(value or '')

	async def switch_to_window(self, handle: str) -> None:
		await self._session_request('POST', '/window', json={'handle': handle})

	async def close_window(self) -> None:
		await self._session_request('DELETE', '/window')

	async def new_window(self, kind: str = 'tab') -> str:
		try:
			value = await self._session_request('POST', '/window/new', json={'type': kind})
			handle = value.get('handle') if isinstance(value, dict) else None
			if handle:
				return str(handle)
		except WebDriverError:
			handles_before = set(await self.window_handles())
			await self.execute_script('window.open("about:blank", "_blank");')
			for _ in range(20):
				handles_after = set(await self.window_handles())
				new_handles = handles_after - handles_before
				if new_handles:
					return sorted(new_handles)[0]
				await asyncio.sleep(0.1)
			raise
		raise WebDriverError(f'Unexpected new window response: {value!r}')

	async def screenshot_base64(self) -> str:
		value = await self._session_request('GET', '/screenshot')
		return str(value or '')

	async def screenshot_bytes(self) -> bytes:
		return base64.b64decode(await self.screenshot_base64())

	async def print_page(self, options: dict[str, Any] | None = None) -> str:
		"""Request a WebDriver print-to-PDF payload when the driver supports it."""
		value = await self._session_request('POST', '/print', json=options or {})
		return str(value or '')

	async def execute_script(self, script: str, args: Sequence[Any] | None = None) -> Any:
		return await self._session_request('POST', '/execute/sync', json={'script': script, 'args': list(args or [])})

	async def execute_async_script(self, script: str, args: Sequence[Any] | None = None) -> Any:
		return await self._session_request('POST', '/execute/async', json={'script': script, 'args': list(args or [])})

	async def find_element(self, using: str, value: str) -> str:
		result = await self._session_request('POST', '/element', json={'using': using, 'value': value})
		element_id = self._element_id_from_result(result)
		if not element_id:
			raise WebDriverError(f'Element not found for {using}={value!r}: {result!r}')
		return element_id

	async def find_element_deep_css(self, value: str) -> str:
		"""Find an element in the document, open shadow roots, or same-origin iframes."""
		result = await self.execute_script(
			"""
			const selector = arguments[0];
			function findDeep(root) {
				if (!root) return null;
				const direct = root.querySelector?.(selector);
				if (direct) return direct;
				const nodes = Array.from(root.querySelectorAll?.('*') || []);
				for (const node of nodes) {
					if (node.shadowRoot) {
						const shadowMatch = findDeep(node.shadowRoot);
						if (shadowMatch) return shadowMatch;
					}
					if (node.tagName === 'IFRAME') {
						try {
							const frameDocument = node.contentDocument;
							const frameMatch = findDeep(frameDocument);
							if (frameMatch) return frameMatch;
						} catch (error) {}
					}
				}
				return null;
			}
			return findDeep(document);
			""",
			[value],
		)
		element_id = self._element_id_from_result(result)
		if not element_id:
			raise WebDriverError(f'Element not found for deep css selector={value!r}: {result!r}')
		return element_id

	async def click_element(self, element_id: str) -> None:
		await self._session_request('POST', f'/element/{element_id}/click', json={})

	async def clear_element(self, element_id: str) -> None:
		await self._session_request('POST', f'/element/{element_id}/clear', json={})

	async def type_element(self, element_id: str, text: str) -> None:
		await self._session_request('POST', f'/element/{element_id}/value', json={'text': text, 'value': list(text)})

	async def send_keys_to_active_element(self, text: str) -> None:
		await self._session_request('POST', '/keys', json={'text': text, 'value': list(text)})

	async def element_value(self, element_id: str) -> Any:
		return await self.execute_script(
			'return arguments[0].value ?? arguments[0].textContent ?? "";',
			[{WEBDRIVER_ELEMENT_KEY: element_id}],
		)

	async def get_cookies(self) -> list[dict[str, Any]]:
		value = await self._session_request('GET', '/cookie')
		return [dict(cookie) for cookie in (value or []) if isinstance(cookie, dict)]

	async def add_cookie(self, cookie: dict[str, Any]) -> None:
		await self._session_request('POST', '/cookie', json={'cookie': cookie})

	async def delete_all_cookies(self) -> None:
		await self._session_request('DELETE', '/cookie')

	async def go_back(self) -> None:
		await self._session_request('POST', '/back', json={})

	async def go_forward(self) -> None:
		await self._session_request('POST', '/forward', json={})

	async def refresh(self) -> None:
		await self._session_request('POST', '/refresh', json={})

	async def _session_request(self, method: str, path: str, **kwargs: Any) -> Any:
		if not self.session_id:
			raise WebDriverError('No active Safari WebDriver session')
		return await self._request(method, f'/session/{self.session_id}{path}', **kwargs)

	async def _request(self, method: str, path: str, session_required: bool = True, **kwargs: Any) -> Any:
		if session_required and not self._client:
			raise WebDriverError('Safari WebDriver client has not been started')
		if self._client is None:
			self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self.config.request_timeout)

		response = await self._client.request(method, path, **kwargs)
		payload: dict[str, Any] | None
		try:
			payload = response.json()
		except ValueError:
			payload = None

		if response.status_code >= 400:
			raise WebDriverError(self._format_error(response.status_code, payload, response.text))

		if payload is None:
			return None

		value = payload.get('value', payload)
		if isinstance(value, dict) and value.get('error'):
			raise WebDriverError(self._format_error(response.status_code, payload, response.text))
		return value

	def _format_error(self, status_code: int, payload: dict[str, Any] | None, text: str) -> str:
		if payload and isinstance(payload.get('value'), dict):
			value = payload['value']
			message = value.get('message') or value.get('error') or text
			return f'WebDriver {status_code}: {message}'
		return f'WebDriver {status_code}: {text}'

	def _element_id_from_result(self, result: Any) -> str | None:
		if isinstance(result, dict):
			if WEBDRIVER_ELEMENT_KEY in result:
				return str(result[WEBDRIVER_ELEMENT_KEY])
			if 'ELEMENT' in result:
				return str(result['ELEMENT'])
		return None
