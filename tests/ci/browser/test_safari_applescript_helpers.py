"""Unit tests for Safari AppleScript helper functions."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

import safari_session.applescript as applescript_module
from safari_session.applescript import AppleScriptResult


@pytest.mark.asyncio
async def test_safari_get_downloads_folder_returns_posix_path(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Downloads folder helper should return the AppleScript POSIX path output."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 5.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='/Users/test/Downloads/', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	path = await applescript_module.safari_get_downloads_folder()
	assert path == '/Users/test/Downloads/'


@pytest.mark.asyncio
async def test_safari_get_downloads_folder_raises_on_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Downloads folder helper should fail when AppleScript returns no path."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 5.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	with pytest.raises(RuntimeError, match='empty downloads folder path'):
		await applescript_module.safari_get_downloads_folder()


@pytest.mark.asyncio
async def test_safari_show_downloads_ui_returns_false_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Downloads UI helper should return False when AppleScript fails."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 2.5) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='', stderr='access denied', return_code=1)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	shown = await applescript_module.safari_show_downloads_ui()
	assert shown is False


@pytest.mark.asyncio
async def test_safari_show_downloads_ui_returns_true_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Downloads UI helper should return True when AppleScript succeeds."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 2.5) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='ok', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	shown = await applescript_module.safari_show_downloads_ui()
	assert shown is True


@pytest.mark.asyncio
async def test_safari_get_file_menu_items_filters_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
	"""File-menu helper should skip separators represented as `missing value`."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 5.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(
			stdout='New Personal Window\nmissing value\nNew School Window\n',
			stderr='',
			return_code=0,
		)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	items = await applescript_module.safari_get_file_menu_items()
	assert items == ['New Personal Window', 'New School Window']


@pytest.mark.asyncio
async def test_safari_open_profile_window_clicks_matching_file_menu_item(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Profile-window helper should match and click the corresponding File menu item."""
	clicked: list[str] = []

	async def fake_get_file_menu_items(timeout_seconds: float = 5.0) -> list[str]:
		del timeout_seconds
		return ['New Personal Window', 'New School Window', 'New Private Window']

	async def fake_click_file_menu_item(item_name: str, timeout_seconds: float = 5.0) -> bool:
		del timeout_seconds
		clicked.append(item_name)
		return True

	monkeypatch.setattr(applescript_module, 'safari_get_file_menu_items', fake_get_file_menu_items)
	monkeypatch.setattr(applescript_module, 'safari_click_file_menu_item', fake_click_file_menu_item)
	selected = await applescript_module.safari_open_profile_window('school')
	assert selected == 'New School Window'
	assert clicked == ['New School Window']


@pytest.mark.asyncio
async def test_safari_open_profile_window_raises_for_unknown_profile(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Profile-window helper should raise with available options when no match exists."""

	async def fake_get_file_menu_items(timeout_seconds: float = 5.0) -> list[str]:
		del timeout_seconds
		return ['New Personal Window', 'New School Window', 'New Private Window']

	monkeypatch.setattr(applescript_module, 'safari_get_file_menu_items', fake_get_file_menu_items)
	with pytest.raises(RuntimeError, match='Available profile windows'):
		await applescript_module.safari_open_profile_window('Work')


@pytest.mark.asyncio
async def test_safari_list_recent_downloads_parses_entries(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Recent-download helper should parse newline-separated full paths."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 5.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(
			stdout='/Users/test/Downloads/a.pdf\n/Users/test/Downloads/b.zip\n',
			stderr='',
			return_code=0,
		)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	entries = await applescript_module.safari_list_recent_downloads(limit=10)
	assert len(entries) == 2
	assert entries[0].file_name == 'a.pdf'
	assert entries[0].path == '/Users/test/Downloads/a.pdf'
	assert entries[1].file_name == 'b.zip'


@pytest.mark.asyncio
async def test_safari_switch_tab_returns_true_on_ok(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Tab-switch helper should return True when AppleScript reports success."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 5.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='ok', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	switched = await applescript_module.safari_switch_tab(2)
	assert switched is True


@pytest.mark.asyncio
async def test_safari_close_tab_returns_false_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Tab-close helper should return False when AppleScript reports missing tab."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 5.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='missing', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	closed = await applescript_module.safari_close_tab(10)
	assert closed is False


@pytest.mark.asyncio
async def test_safari_get_tabs_parses_title_url_pairs(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Tab list helper should parse valid `title|url` lines and skip malformed rows."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 10.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(
			stdout='One|https://one.example\ninvalid-line\nTwo|https://two.example\n',
			stderr='',
			return_code=0,
		)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	tabs = await applescript_module.safari_get_tabs()
	assert len(tabs) == 2
	assert tabs[0].title == 'One'
	assert tabs[0].url == 'https://one.example'
	assert tabs[1].title == 'Two'
	assert tabs[1].url == 'https://two.example'


@pytest.mark.asyncio
async def test_safari_get_tabs_raises_when_applescript_fails(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Tab list helper should raise when AppleScript execution fails."""

	async def fake_run_applescript(script: str, timeout_seconds: float = 10.0) -> AppleScriptResult:
		del script, timeout_seconds
		return AppleScriptResult(stdout='', stderr='permission denied', return_code=1)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	with pytest.raises(RuntimeError, match='AppleScript failed'):
		await applescript_module.safari_get_tabs()


@pytest.mark.asyncio
async def test_safari_open_tab_escapes_quotes_in_url(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Open-tab helper should escape quotes before embedding URL in AppleScript."""
	captured_scripts: list[str] = []

	async def fake_run_applescript(script: str, timeout_seconds: float = 10.0) -> AppleScriptResult:
		del timeout_seconds
		captured_scripts.append(script)
		return AppleScriptResult(stdout='', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	await applescript_module.safari_open_tab('https://example.com/?q="quoted"')

	assert captured_scripts
	assert '\\"quoted\\"' in captured_scripts[0]


@pytest.mark.asyncio
async def test_safari_execute_js_returns_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Execute-JS helper should return raw AppleScript stdout on success."""
	captured_scripts: list[str] = []

	async def fake_run_applescript(script: str, timeout_seconds: float = 10.0) -> AppleScriptResult:
		del timeout_seconds
		captured_scripts.append(script)
		return AppleScriptResult(stdout='42', stderr='', return_code=0)

	monkeypatch.setattr(applescript_module, 'run_applescript', fake_run_applescript)
	value = await applescript_module.safari_execute_js('return "ok";')
	assert value == '42'
	assert captured_scripts
	assert '\\"ok\\"' in captured_scripts[0]


@pytest.mark.asyncio
async def test_run_applescript_wraps_subprocess_result(monkeypatch: pytest.MonkeyPatch) -> None:
	"""run_applescript should surface stdout/stderr/return code from subprocess."""

	class _Completed:
		def __init__(self, stdout: str, stderr: str, returncode: int) -> None:
			self.stdout = stdout
			self.stderr = stderr
			self.returncode = returncode

	def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: float) -> Any:
		assert cmd[0] == 'osascript'
		assert capture_output is True
		assert text is True
		assert timeout == 10.0
		return _Completed(stdout=' out \n', stderr=' err \n', returncode=7)

	monkeypatch.setattr(applescript_module.subprocess, 'run', fake_run)
	result = await applescript_module.run_applescript('return "x"')
	assert result.stdout == 'out'
	assert result.stderr == 'err'
	assert result.return_code == 7


@pytest.mark.asyncio
async def test_run_applescript_raises_timeout_error(monkeypatch: pytest.MonkeyPatch) -> None:
	"""run_applescript should raise TimeoutError when osascript exceeds timeout."""

	def fake_run(cmd: list[str], capture_output: bool, text: bool, timeout: float) -> Any:
		del cmd, capture_output, text, timeout
		raise subprocess.TimeoutExpired(cmd='osascript', timeout=1.0)

	monkeypatch.setattr(applescript_module.subprocess, 'run', fake_run)
	with pytest.raises(TimeoutError, match='AppleScript timed out'):
		await applescript_module.run_applescript('return "x"', timeout_seconds=1.0)
