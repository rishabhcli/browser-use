"""Safari backend diagnostics and doctor probes."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from browser_use.browser.backends.safari.webdriver_client import (
	SAFARI_DRIVER,
	SAFARI_TP_DRIVER,
	SafariDriverConfig,
	SafariWebDriverClient,
)


async def run_safari_doctor(run_session_probe: bool = True) -> dict[str, Any]:
	"""Run Safari backend diagnostics.

	The doctor is intentionally STP-first because that is the safest target for
	development and avoids disturbing the user's regular Safari profile.
	"""
	checks: dict[str, dict[str, Any]] = {
		'technology_preview_driver': _driver_check(SAFARI_TP_DRIVER),
		'safari_driver': _driver_check(SAFARI_DRIVER),
	}

	if run_session_probe and checks['technology_preview_driver']['status'] == 'ok':
		checks['technology_preview_session'] = await _session_probe(SAFARI_TP_DRIVER, 'Safari Technology Preview')
	elif run_session_probe and checks['safari_driver']['status'] == 'ok':
		checks['safari_session'] = await _session_probe(SAFARI_DRIVER, 'Safari')
	else:
		checks['session'] = {
			'status': 'missing',
			'message': 'No Safari driver found for session probe',
		}

	status = 'healthy' if all(item.get('status') == 'ok' for item in checks.values()) else 'issues_found'
	return {
		'status': status,
		'checks': checks,
		'summary': _summary(checks),
	}


def _driver_check(path: Path) -> dict[str, Any]:
	if path.exists():
		return {
			'status': 'ok',
			'message': f'found {path}',
			'path': str(path),
		}
	return {
		'status': 'missing',
		'message': f'not found at {path}',
		'path': str(path),
	}


async def _session_probe(driver_path: Path, browser_name: str) -> dict[str, Any]:
	last_error: Exception | None = None
	for attempt in range(3):
		result = await _session_probe_once(driver_path, browser_name)
		if result.get('status') == 'ok':
			return result
		last_error = result.get('exception') if isinstance(result.get('exception'), Exception) else last_error
		if attempt < 2:
			await asyncio.sleep(1.0)

	message = f'{browser_name} WebDriver probe failed'
	if last_error:
		message += f': {type(last_error).__name__}: {last_error}'
	return {
		'status': 'error',
		'message': message,
		'fix': 'Enable Safari remote automation if needed: Safari > Settings > Advanced > Show features for web developers, then Develop > Allow Remote Automation.',
	}


async def _session_probe_once(driver_path: Path, browser_name: str) -> dict[str, Any]:
	client = SafariWebDriverClient(
		SafariDriverConfig(driver_path=driver_path, browser_name=browser_name, start_timeout=8.0, request_timeout=8.0)
	)
	try:
		await client.start_driver()
		capabilities = await client.create_session()
		await client.navigate('data:text/html,<title>Browser Use Safari Doctor</title><button id="ok">OK</button>')
		title = await client.title()
		url = await client.current_url()
		screenshot = await client.screenshot_bytes()
		js_result = await client.execute_script('return document.querySelector("#ok").textContent;')
		return {
			'status': 'ok',
			'message': f'{browser_name} WebDriver session works',
			'capabilities': capabilities,
			'title': title,
			'url': url,
			'screenshot_bytes': len(screenshot),
			'js_result': js_result,
		}
	except Exception as exc:
		return {
			'status': 'error',
			'message': f'{browser_name} WebDriver probe failed: {type(exc).__name__}: {exc}',
			'exception': exc,
			'fix': 'Enable Safari remote automation if needed: Safari > Settings > Advanced > Show features for web developers, then Develop > Allow Remote Automation.',
		}
	finally:
		with contextlib.suppress(Exception):
			await client.close(force=True)


def _summary(checks: dict[str, dict[str, Any]]) -> str:
	ok = sum(1 for item in checks.values() if item.get('status') == 'ok')
	missing = sum(1 for item in checks.values() if item.get('status') == 'missing')
	error = sum(1 for item in checks.values() if item.get('status') == 'error')
	total = len(checks)
	parts = [f'{ok}/{total} checks passed']
	if missing:
		parts.append(f'{missing} missing')
	if error:
		parts.append(f'{error} errors')
	return ', '.join(parts)
