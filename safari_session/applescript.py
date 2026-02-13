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
		completed = subprocess.run(
			['osascript', '-e', script],
			capture_output=True,
			text=True,
		)
		return AppleScriptResult(
			stdout=completed.stdout.strip(),
			stderr=completed.stderr.strip(),
			return_code=completed.returncode,
		)

	return await asyncio.wait_for(asyncio.to_thread(_run), timeout=timeout_seconds)


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
	escaped_url = url.replace('"', '\\"')
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
	escaped_js = js_code.replace('\\', '\\\\').replace('"', '\\"')
	script = f"""
	tell application "Safari"
		return do JavaScript "{escaped_js}" in document 1
	end tell
	"""
	result = await run_applescript(script, timeout_seconds=timeout_seconds)
	if not result.ok:
		raise RuntimeError(f'AppleScript failed: {result.stderr or result.stdout}')
	return result.stdout
