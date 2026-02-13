"""AppleScript helper utilities for Safari-specific augmentations."""

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, validate_call


class AppleScriptResult(BaseModel):
	"""Execution result for an AppleScript command."""

	model_config = ConfigDict(extra='forbid')

	stdout: str
	stderr: str
	return_code: int

	@property
	def ok(self) -> bool:
		return self.return_code == 0


class SafariTabEntry(BaseModel):
	"""Basic tab descriptor returned by AppleScript helpers."""

	model_config = ConfigDict(extra='forbid')

	title: str
	url: str


class SafariDownloadEntry(BaseModel):
	"""Basic download descriptor from Safari/OS download folder inspection."""

	model_config = ConfigDict(extra='forbid')

	file_name: str
	path: str


def _escape_applescript_string(value: str) -> str:
	"""Escape a Python string so it can be embedded in AppleScript string literals."""
	return value.replace('\\', '\\\\').replace('"', '\\"')


def _parse_applescript_lines(raw_output: str) -> list[str]:
	"""Parse non-empty AppleScript output lines, skipping separator placeholders."""
	items: list[str] = []
	for line in raw_output.splitlines():
		text = line.strip()
		if not text:
			continue
		if text.lower() == 'missing value':
			continue
		items.append(text)
	return items


def _parse_download_lines(raw_output: str) -> list[SafariDownloadEntry]:
	"""Parse newline-separated download file paths from AppleScript output."""
	entries: list[SafariDownloadEntry] = []
	for line in raw_output.splitlines():
		path = line.strip()
		if not path:
			continue
		file_name = Path(path).name
		if not file_name:
			continue
		entries.append(SafariDownloadEntry(file_name=file_name, path=path))
	return entries


@validate_call
async def run_applescript(script: str, timeout_seconds: float = 10.0) -> AppleScriptResult:
	"""Run an AppleScript snippet via `osascript`."""

	def _run() -> AppleScriptResult:
		try:
			completed = subprocess.run(
				['osascript', '-e', script],
				capture_output=True,
				text=True,
				timeout=timeout_seconds,
			)
		except subprocess.TimeoutExpired as exc:
			raise TimeoutError(f'AppleScript timed out after {timeout_seconds:.1f}s') from exc
		return AppleScriptResult(
			stdout=completed.stdout.strip(),
			stderr=completed.stderr.strip(),
			return_code=completed.returncode,
		)

	return await asyncio.to_thread(_run)


async def safari_get_tabs(timeout_seconds: float = 10.0) -> list[SafariTabEntry]:
	"""Return open Safari tabs via AppleScript."""
	script = """
	tell application "Safari"
		set output to ""
		repeat with w in windows
			repeat with t in tabs of w
				set output to output & (name of t) & "|" & (URL of t) & linefeed
			end repeat
		end repeat
		return output
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')

	entries: list[SafariTabEntry] = []
	for line in result.stdout.splitlines():
		if not line.strip():
			continue
		title, sep, url = line.partition('|')
		if not sep:
			continue
		entries.append(SafariTabEntry(title=title.strip(), url=url.strip()))
	return entries


async def safari_get_downloads_folder(timeout_seconds: float = 5.0) -> str:
	"""Get the current macOS downloads folder as a POSIX path."""
	script = 'return POSIX path of (path to downloads folder)'
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	path = result.stdout.strip()
	if not path:
		raise RuntimeError('AppleScript returned an empty downloads folder path')
	return path


async def safari_show_downloads_ui(timeout_seconds: float = 2.5) -> bool:
	"""Best-effort: bring Safari to foreground and open downloads UI."""
	script = """
	tell application "Safari" to activate
	tell application "System Events"
		tell process "Safari"
			keystroke "l" using {command down, option down}
		end tell
	end tell
	return "ok"
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	return result.ok


@validate_call
async def safari_get_file_menu_items(timeout_seconds: float = 5.0) -> list[str]:
	"""Return visible item names from Safari's File menu."""
	script = """
	tell application "Safari" to activate
	delay 0.1
	tell application "System Events"
		tell process "Safari"
			set output to ""
			repeat with itemRef in menu items of menu "File" of menu bar 1
				set itemName to name of itemRef as text
				if itemName is not missing value then
					set output to output & itemName & linefeed
				end if
			end repeat
			return output
		end tell
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return _parse_applescript_lines(result.stdout)


@validate_call
async def safari_click_file_menu_item(item_name: str, timeout_seconds: float = 5.0) -> bool:
	"""Click a Safari File menu item by exact label. Returns False if item is missing."""
	escaped_name = _escape_applescript_string(item_name)
	script = f"""
	set targetItem to "{escaped_name}"
	tell application "Safari" to activate
	delay 0.1
	tell application "System Events"
		tell process "Safari"
			if exists menu item targetItem of menu "File" of menu bar 1 then
				click menu item targetItem of menu "File" of menu bar 1
				return "ok"
			end if
		end tell
	end tell
	return "missing"
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return result.stdout.strip().lower() == 'ok'


@validate_call
async def safari_open_profile_window(profile_name: str, timeout_seconds: float = 6.0) -> str:
	"""Open a Safari profile window by profile name and return the clicked menu item label."""
	normalized = profile_name.strip()
	if not normalized:
		raise ValueError('profile_name must not be empty')

	# Accept either "School" or "New School Window".
	wanted_item = normalized
	wanted_lower = wanted_item.lower()
	if not (wanted_lower.startswith('new ') and wanted_lower.endswith(' window')):
		wanted_item = f'New {normalized} Window'
		wanted_lower = wanted_item.lower()

	file_menu_items = await safari_get_file_menu_items(timeout_seconds=min(timeout_seconds, 5.0))
	menu_items_by_lower = {name.lower(): name for name in file_menu_items}
	selected_item = menu_items_by_lower.get(wanted_lower)

	if selected_item is None:
		profile_window_items = [
			name
			for name in file_menu_items
			if name.lower().startswith('new ') and name.lower().endswith(' window') and name.lower() != 'new private window'
		]
		fuzzy_matches = [name for name in profile_window_items if normalized.lower() in name.lower()]
		if len(fuzzy_matches) == 1:
			selected_item = fuzzy_matches[0]
		else:
			available_profiles = ', '.join(profile_window_items) if profile_window_items else '<none>'
			raise RuntimeError(
				f'No Safari profile menu item matched "{profile_name}". Available profile windows: {available_profiles}'
			)

	clicked = await safari_click_file_menu_item(selected_item, timeout_seconds=timeout_seconds)
	if not clicked:
		raise RuntimeError(f'Safari File menu item "{selected_item}" is not available')
	return selected_item


@validate_call
async def safari_list_recent_downloads(limit: int = 25, timeout_seconds: float = 5.0) -> list[SafariDownloadEntry]:
	"""List recent downloaded files from the user's Downloads folder."""
	limit = max(1, min(limit, 200))
	script = f"""
	set downloadsPath to POSIX path of (path to downloads folder)
	set shellCmd to "ls -1tp " & quoted form of downloadsPath & " | grep -v '/$' | head -n {limit}"
	set rawOutput to ""
	try
		set rawOutput to do shell script shellCmd
	on error
		set rawOutput to ""
	end try
	if rawOutput is "" then return ""
	set output to ""
	repeat with itemName in paragraphs of rawOutput
		set currentName to (itemName as text)
		if currentName is not "" then
			set output to output & downloadsPath & currentName & linefeed
		end if
	end repeat
	return output
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return _parse_download_lines(result.stdout)


@validate_call
async def safari_open_tab(url: str, timeout_seconds: float = 10.0) -> None:
	"""Open a new tab in Safari with a URL using AppleScript."""
	escaped_url = _escape_applescript_string(url)
	script = f"""
	tell application "Safari"
		tell window 1
			set current tab to (make new tab with properties {{URL:"{escaped_url}"}})
		end tell
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')


@validate_call
async def safari_switch_tab(tab_index: int, timeout_seconds: float = 5.0) -> bool:
	"""Switch the active Safari tab in window 1 by zero-based index."""
	target_index = tab_index + 1
	script = f"""
	set targetIndex to {target_index}
	tell application "Safari"
		if (count windows) is 0 then return "no-window"
		tell window 1
			if targetIndex > (count tabs) then return "missing"
			set current tab to tab targetIndex
			return "ok"
		end tell
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return result.stdout.strip().lower() == 'ok'


@validate_call
async def safari_close_tab(tab_index: int, timeout_seconds: float = 5.0) -> bool:
	"""Close a Safari tab in window 1 by zero-based index."""
	target_index = tab_index + 1
	script = f"""
	set targetIndex to {target_index}
	tell application "Safari"
		if (count windows) is 0 then return "no-window"
		tell window 1
			if targetIndex > (count tabs) then return "missing"
			set tabCountBefore to (count tabs)
			close tab targetIndex
			if (count tabs) < tabCountBefore then return "ok"
			return "unknown"
		end tell
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return result.stdout.strip().lower() == 'ok'


@validate_call
async def safari_execute_js(js_code: str, timeout_seconds: float = 10.0) -> Any:
	"""Execute JavaScript in the active Safari document via AppleScript."""
	escaped_js = _escape_applescript_string(js_code)
	script = f"""
	tell application "Safari"
		return do JavaScript "{escaped_js}" in document 1
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return result.stdout
