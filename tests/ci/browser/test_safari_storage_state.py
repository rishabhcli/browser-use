"""Tests for Safari storage state save/load behavior."""

import json
from pathlib import Path
from typing import Any

import pytest

from browser_use.browser.profile import BrowserProfile
from safari_session import SafariBrowserSession


@pytest.mark.asyncio
async def test_safari_save_storage_state_writes_file(tmp_path: Path) -> None:
	"""Saving storage state should persist cookies and web storage to disk."""
	state_path = tmp_path / 'safari-state.json'
	session = SafariBrowserSession(
		browser_profile=BrowserProfile(
			storage_state=str(state_path),
			user_data_dir='/tmp/browser-use-safari-storage-save',
		)
	)
	session.driver._driver = object()

	async def fake_get_cookies() -> list[dict[str, Any]]:
		return [{'name': 'sid', 'value': 'abc', 'domain': 'example.com', 'path': '/'}]

	async def fake_execute_js(script: str, *args: Any) -> dict[str, Any]:
		del script, args
		return {'localStorage': {'k1': 'v1'}, 'sessionStorage': {'k2': 'v2'}}

	session.driver.get_cookies = fake_get_cookies  # type: ignore[method-assign]
	session.driver.execute_js = fake_execute_js  # type: ignore[method-assign]

	try:
		await session.save_storage_state()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert state_path.exists()
	data = json.loads(state_path.read_text())
	assert data['cookies'][0]['name'] == 'sid'
	assert data['localStorage'] == {'k1': 'v1'}
	assert data['sessionStorage'] == {'k2': 'v2'}


@pytest.mark.asyncio
async def test_safari_load_storage_state_replays_cookies_and_storage(tmp_path: Path) -> None:
	"""Loading storage state should restore cookies and local/session storage."""
	state_path = tmp_path / 'safari-state.json'
	state_path.write_text(
		json.dumps(
			{
				'cookies': [
					{'name': 'sid', 'value': 'abc', 'domain': '.example.com', 'path': '/'},
					{'name': 'prefs', 'value': 'dark', 'domain': 'www.example.com', 'path': '/'},
				],
				'localStorage': {'a': '1'},
				'sessionStorage': {'b': '2'},
			}
		)
	)

	session = SafariBrowserSession(
		browser_profile=BrowserProfile(
			storage_state=str(state_path),
			user_data_dir='/tmp/browser-use-safari-storage-load',
		)
	)
	session.driver._driver = object()

	navigated_urls: list[str] = []
	set_cookies: list[dict[str, Any]] = []
	storage_payloads: list[tuple[dict[str, Any], dict[str, Any]]] = []

	async def fake_navigate(url: str) -> str:
		navigated_urls.append(url)
		return url

	async def fake_set_cookie(cookie: dict[str, Any]) -> None:
		set_cookies.append(cookie)

	async def fake_execute_js(script: str, *args: Any) -> bool:
		del script
		if len(args) >= 2 and isinstance(args[0], dict) and isinstance(args[1], dict):
			storage_payloads.append((args[0], args[1]))
		return True

	session.driver.navigate = fake_navigate  # type: ignore[method-assign]
	session.driver.set_cookie = fake_set_cookie  # type: ignore[method-assign]
	session.driver.execute_js = fake_execute_js  # type: ignore[method-assign]

	try:
		await session.load_storage_state()
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert any(url.startswith('https://example.com') for url in navigated_urls)
	assert any(url.startswith('https://www.example.com') for url in navigated_urls)
	assert len(set_cookies) == 2
	assert storage_payloads
	assert storage_payloads[-1][0] == {'a': '1'}
	assert storage_payloads[-1][1] == {'b': '2'}
