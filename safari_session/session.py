"""Safari BrowserSession-compatible adapter.

This adapter translates Browser Use's event-driven browser actions to Safari via
safaridriver + JavaScript injection, with a small CDP-compatible shim for tools
that still call Runtime.evaluate and Page.getLayoutMetrics.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
from bubus import EventBus
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, validate_call
from uuid_extensions import uuid7str

from browser_use.browser.events import (
	BrowserStateRequestEvent,
	ClickCoordinateEvent,
	ClickElementEvent,
	CloseTabEvent,
	FileDownloadedEvent,
	GetDropdownOptionsEvent,
	GoBackEvent,
	GoForwardEvent,
	NavigateToUrlEvent,
	RefreshEvent,
	ScreenshotEvent,
	ScrollEvent,
	ScrollToTextEvent,
	SelectDropdownOptionEvent,
	SendKeysEvent,
	SwitchTabEvent,
	TypeTextEvent,
	UploadFileEvent,
	WaitEvent,
)
from browser_use.browser.profile import BrowserProfile
from browser_use.browser.views import BrowserError, BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, EnhancedSnapshotNode, NodeType, SerializedDOMState, SimplifiedNode
from safari_session.applescript import (
	SafariDownloadEntry,
	SafariTabEntry,
	safari_close_tab,
	safari_get_downloads_folder,
	safari_get_tabs,
	safari_list_recent_downloads,
	safari_open_profile_window,
	safari_open_tab,
	safari_show_downloads_ui,
	safari_switch_tab,
)
from safari_session.dom_extractor import SafariDOMExtractionResult, SafariExtractedElement, extract_interactive_elements
from safari_session.driver import SafariDriver, SafariDriverConfig

# Ensure event models with forward refs are fully resolved.
ClickElementEvent.model_rebuild()
TypeTextEvent.model_rebuild()
ScrollEvent.model_rebuild()
UploadFileEvent.model_rebuild()
GetDropdownOptionsEvent.model_rebuild()
SelectDropdownOptionEvent.model_rebuild()

_STABLE_ID_ATTR = 'data-browser-use-stable-id'


def _normalize_storage_path(path: str | Path) -> str:
	"""Normalize storage-state path outside async contexts."""
	return os.path.abspath(os.path.expanduser(str(path)))


@dataclass(slots=True)
class _SafariElementRef:
	backend_node_id: int
	stable_id: str
	tag_name: str
	text_content: str
	attributes: dict[str, str]
	css_selector: str | None
	xpath: str | None
	absolute_position: DOMRect | None


class _RuntimeCommandShim:
	"""Subset of CDP Runtime.* commands used by Browser Use tools."""

	def __init__(self, safari_session: SafariBrowserSession):
		self._safari_session = safari_session

	async def evaluate(self, params: dict[str, Any], session_id: str | None = None) -> dict[str, Any]:
		del session_id
		expression = str(params.get('expression', ''))
		await_promise = bool(params.get('awaitPromise', False))

		if not expression:
			return {'result': {'type': 'undefined'}}

		try:
			if await_promise:
				result = await self._safari_session.driver.execute_async_js(
					"""
					const expression = arguments[0];
					const callback = arguments[arguments.length - 1];
					try {
						Promise.resolve(eval(expression)).then(
							(value) => callback({ ok: true, value }),
							(error) => callback({ ok: false, error: String(error && error.message ? error.message : error) })
						);
					} catch (error) {
						callback({ ok: false, error: String(error && error.message ? error.message : error) });
					}
					""",
					expression,
				)
				if not isinstance(result, dict) or not result.get('ok'):
					error_text = 'Unknown JavaScript error'
					if isinstance(result, dict):
						error_text = str(result.get('error', error_text))
					return {
						'result': {'type': 'undefined'},
						'exceptionDetails': {'text': error_text},
					}
				value = result.get('value')
			else:
				value = await self._safari_session.driver.execute_js('return eval(arguments[0]);', expression)

			return {
				'result': {
					'type': type(value).__name__.lower() if value is not None else 'undefined',
					'value': value,
				}
			}
		except Exception as exc:
			return {
				'result': {'type': 'undefined'},
				'exceptionDetails': {'text': str(exc)},
			}


class _PageCommandShim:
	"""Subset of CDP Page.* commands used by Browser Use tools."""

	def __init__(self, safari_session: SafariBrowserSession):
		self._safari_session = safari_session

	async def getLayoutMetrics(self, session_id: str | None = None) -> dict[str, Any]:
		del session_id
		metrics = await self._safari_session.driver.execute_js(
			"""
			return {
				viewport_width: window.innerWidth || document.documentElement.clientWidth || 0,
				viewport_height: window.innerHeight || document.documentElement.clientHeight || 0,
				page_width: Math.max(
					document.body ? document.body.scrollWidth : 0,
					document.documentElement ? document.documentElement.scrollWidth : 0,
					window.innerWidth || 0
				),
				page_height: Math.max(
					document.body ? document.body.scrollHeight : 0,
					document.documentElement ? document.documentElement.scrollHeight : 0,
					window.innerHeight || 0
				),
				scroll_x: window.scrollX || window.pageXOffset || document.documentElement.scrollLeft || 0,
				scroll_y: window.scrollY || window.pageYOffset || document.documentElement.scrollTop || 0
			};
			"""
		)
		if not isinstance(metrics, dict):
			metrics = {}

		viewport_width = int(metrics.get('viewport_width', 0) or 0)
		viewport_height = int(metrics.get('viewport_height', 0) or 0)
		page_width = int(metrics.get('page_width', viewport_width) or viewport_width)
		page_height = int(metrics.get('page_height', viewport_height) or viewport_height)
		scroll_x = int(metrics.get('scroll_x', 0) or 0)
		scroll_y = int(metrics.get('scroll_y', 0) or 0)

		return {
			'cssVisualViewport': {
				'offsetX': scroll_x,
				'offsetY': scroll_y,
				'pageX': scroll_x,
				'pageY': scroll_y,
				'clientWidth': viewport_width,
				'clientHeight': viewport_height,
				'scale': 1,
				'zoom': 1,
			},
			'cssLayoutViewport': {
				'pageX': scroll_x,
				'pageY': scroll_y,
				'clientWidth': viewport_width,
				'clientHeight': viewport_height,
			},
			'cssContentSize': {
				'x': 0,
				'y': 0,
				'width': page_width,
				'height': page_height,
			},
		}


class _SendShim:
	def __init__(self, safari_session: SafariBrowserSession):
		self.Runtime = _RuntimeCommandShim(safari_session)
		self.Page = _PageCommandShim(safari_session)


class _CDPClientShim:
	def __init__(self, safari_session: SafariBrowserSession):
		self.send = _SendShim(safari_session)


@dataclass(slots=True)
class _CDPSessionShim:
	cdp_client: _CDPClientShim
	target_id: str
	session_id: str = 'safari-session'


class _SafariDOMWatchdogShim:
	"""Minimal DOM watchdog API used by markdown extractor + tools."""

	def __init__(self, safari_session: SafariBrowserSession):
		self._safari_session = safari_session
		self.enhanced_dom_tree: EnhancedDOMTreeNode | None = None

	def clear_cache(self) -> None:
		self.enhanced_dom_tree = None

	async def _build_dom_tree_without_highlights(self) -> None:
		self.enhanced_dom_tree = await self._safari_session._build_text_dom_tree_for_markdown()


class SafariBrowserSession(BaseModel):
	"""Browser Use-compatible Safari session adapter."""

	model_config = ConfigDict(
		arbitrary_types_allowed=True, extra='forbid', validate_assignment=True, revalidate_instances='never'
	)

	id: str = Field(default_factory=uuid7str)
	browser_profile: BrowserProfile = Field(default_factory=BrowserProfile)
	driver: SafariDriver = Field(default_factory=SafariDriver)
	event_bus: EventBus = Field(default_factory=lambda: EventBus(name=f'SafariBrowser_{uuid7str()[-4:]}'))
	is_local: bool = True
	cdp_url: str | None = None
	safari_profile_name: str | None = None
	agent_focus_target_id: str | None = None
	llm_screenshot_size: tuple[int, int] | None = None
	session_manager: Any | None = None

	_original_viewport_size: tuple[int, int] | None = PrivateAttr(default=None)
	_cached_browser_state_summary: BrowserStateSummary | None = PrivateAttr(default=None)
	_cached_selector_map: dict[int, EnhancedDOMTreeNode] = PrivateAttr(default_factory=dict)
	_ref_by_backend_id: dict[int, _SafariElementRef] = PrivateAttr(default_factory=dict)
	_target_to_handle: dict[str, str] = PrivateAttr(default_factory=dict)
	_handle_to_target: dict[str, str] = PrivateAttr(default_factory=dict)
	_tabs_cache: list[TabInfo] = PrivateAttr(default_factory=list)
	_dom_watchdog: _SafariDOMWatchdogShim | None = PrivateAttr(default=None)
	_cdp_client_root: Any | None = PrivateAttr(default=None)
	_cdp_shim: _CDPClientShim | None = PrivateAttr(default=None)
	_downloaded_files: list[str] = PrivateAttr(default_factory=list)
	_closed_popup_messages: list[str] = PrivateAttr(default_factory=list)
	_recent_events: list[str] = PrivateAttr(default_factory=list)
	_download_snapshot: dict[str, tuple[int, float]] | None = PrivateAttr(default=None)
	_applescript_download_dir: str | None = PrivateAttr(default=None)
	_applescript_download_dir_checked: bool = PrivateAttr(default=False)
	_applescript_tabs_cache: list[SafariTabEntry] = PrivateAttr(default_factory=list)
	_applescript_tabs_cached_at: float = PrivateAttr(default=0.0)
	_started: bool = PrivateAttr(default=False)
	_handlers_registered: bool = PrivateAttr(default=False)
	_state_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
	_tab_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)

	@classmethod
	def from_config(cls, config: SafariDriverConfig) -> SafariBrowserSession:
		return cls(driver=SafariDriver(config=config))

	@property
	def logger(self) -> logging.Logger:
		target = self.agent_focus_target_id[-2:] if self.agent_focus_target_id else '--'
		return logging.getLogger(f'browser_use.SafariSession🅑 {self.id[-4:]} 🅣 {target}')

	@property
	def downloaded_files(self) -> list[str]:
		return list(self._downloaded_files)

	@property
	def cdp_client(self) -> Any:
		"""Compatibility shim for code paths that expect BrowserSession.cdp_client."""
		if self._cdp_shim is None:
			self._cdp_shim = _CDPClientShim(self)
		if self._cdp_client_root is None:
			self._cdp_client_root = self._cdp_shim
		return self._cdp_shim

	def _append_recent_event(self, event_label: str) -> None:
		self._recent_events.append(event_label)
		if len(self._recent_events) > 20:
			self._recent_events = self._recent_events[-20:]

	def _recent_events_text(self) -> str | None:
		if not self._recent_events:
			return None
		return '\n'.join(self._recent_events[-10:])

	def _invalidate_dom_cache(self) -> None:
		self._cached_browser_state_summary = None
		self._cached_selector_map.clear()
		self._ref_by_backend_id.clear()
		if self._dom_watchdog:
			self._dom_watchdog.clear_cache()

	def _register_handlers(self) -> None:
		if self._handlers_registered:
			return
		self.event_bus.on(NavigateToUrlEvent, self.on_NavigateToUrlEvent)
		self.event_bus.on(ClickElementEvent, self.on_ClickElementEvent)
		self.event_bus.on(ClickCoordinateEvent, self.on_ClickCoordinateEvent)
		self.event_bus.on(TypeTextEvent, self.on_TypeTextEvent)
		self.event_bus.on(ScrollEvent, self.on_ScrollEvent)
		self.event_bus.on(ScrollToTextEvent, self.on_ScrollToTextEvent)
		self.event_bus.on(ScreenshotEvent, self.on_ScreenshotEvent)
		self.event_bus.on(BrowserStateRequestEvent, self.on_BrowserStateRequestEvent)
		self.event_bus.on(SwitchTabEvent, self.on_SwitchTabEvent)
		self.event_bus.on(CloseTabEvent, self.on_CloseTabEvent)
		self.event_bus.on(GoBackEvent, self.on_GoBackEvent)
		self.event_bus.on(GoForwardEvent, self.on_GoForwardEvent)
		self.event_bus.on(RefreshEvent, self.on_RefreshEvent)
		self.event_bus.on(WaitEvent, self.on_WaitEvent)
		self.event_bus.on(SendKeysEvent, self.on_SendKeysEvent)
		self.event_bus.on(UploadFileEvent, self.on_UploadFileEvent)
		self.event_bus.on(GetDropdownOptionsEvent, self.on_GetDropdownOptionsEvent)
		self.event_bus.on(SelectDropdownOptionEvent, self.on_SelectDropdownOptionEvent)
		self.event_bus.on(FileDownloadedEvent, self.on_FileDownloadedEvent)
		self._handlers_registered = True

	def _is_stale_pairing_error(self, exc: Exception) -> bool:
		error_text = str(exc).lower()
		return (
			'already paired with another webdriver session' in error_text
			or 'already paired with a different session' in error_text
			or 'automation session ended unexpected while attempting to pair' in error_text
		)

	async def _reset_stale_safaridriver_process(self) -> None:
		"""Best-effort cleanup for stale local safaridriver pairings."""
		try:
			await asyncio.to_thread(
				subprocess.run,
				['pkill', '-f', 'safaridriver'],
				check=False,
				capture_output=True,
				text=True,
			)
		except Exception as exc:
			self.logger.debug(f'Failed to reset stale safaridriver process: {exc}')
			return
		await asyncio.sleep(0.4)

	async def _start_driver_with_pairing_recovery(self, timeout_seconds: float) -> None:
		"""Start SafariDriver and recover once from stale pairing failures."""
		try:
			await asyncio.wait_for(self.driver.start(), timeout=timeout_seconds)
			return
		except Exception as exc:
			if not self._is_stale_pairing_error(exc):
				raise
			self.logger.warning(f'Detected stale Safari WebDriver pairing: {exc}. Resetting and retrying once')
			await self._reset_stale_safaridriver_process()
			self.driver._driver = None
			await asyncio.wait_for(self.driver.start(), timeout=timeout_seconds)

	async def _with_retry(self, operation_name: str, operation, retries: int = 2, check_driver_alive: bool = True) -> Any:
		last_error: Exception | None = None
		for attempt in range(retries + 1):
			try:
				if check_driver_alive and not await self.driver.is_alive():
					raise RuntimeError('Safari WebDriver session is not alive')
				return await operation()
			except Exception as exc:  # noqa: PERF203
				last_error = exc
				if attempt == retries:
					break
				await asyncio.sleep(0.15 * (2**attempt))
		raise BrowserError(
			message=f'{operation_name} failed: {last_error}',
			long_term_memory=f'{operation_name} failed in Safari session. Refresh state and retry.',
		)

	async def _dismiss_dialog_if_any(self) -> bool:
		try:
			handled = bool(
				await self._with_retry(
					'dismiss_dialog:handle_dialog',
					lambda: self.driver.handle_dialog(accept=True),
					retries=1,
					check_driver_alive=False,
				)
			)
			if handled:
				self._closed_popup_messages.append('Closed JavaScript dialog automatically.')
				self._append_recent_event('dialog:auto-accepted')
		except Exception:
			return False
		return bool(handled)

	async def _dismiss_dialogs_after_navigation(
		self,
		max_wait_seconds: float = 0.8,
		poll_interval_seconds: float = 0.15,
	) -> bool:
		"""Poll briefly for delayed dialogs (e.g., onbeforeunload) after navigation actions."""
		max_wait_seconds = max(max_wait_seconds, 0.0)
		poll_interval_seconds = max(poll_interval_seconds, 0.0)
		loop = asyncio.get_running_loop()
		deadline = loop.time() + max_wait_seconds

		attempts = 0
		saw_dialog = False
		handled_any = False

		while True:
			handled = await self._dismiss_dialog_if_any()
			handled_any = handled_any or handled
			attempts += 1

			if handled:
				saw_dialog = True
			else:
				if saw_dialog:
					return handled_any
				if attempts >= 2:
					return handled_any

			if loop.time() >= deadline:
				return handled_any
			await asyncio.sleep(poll_interval_seconds)

	async def _refresh_applescript_download_dir(self) -> None:
		"""Best-effort discovery of the current downloads folder via AppleScript."""
		if self._applescript_download_dir_checked:
			return
		self._applescript_download_dir_checked = True
		try:
			path = await safari_get_downloads_folder(timeout_seconds=2.0)
		except Exception as exc:
			self.logger.debug(f'AppleScript downloads folder unavailable: {exc}')
			self._applescript_download_dir = None
			return
		self._applescript_download_dir = path.strip() or None

	def _download_directories(self) -> list[Path]:
		candidates: list[Path] = []
		downloads_path = self.browser_profile.downloads_path
		if isinstance(downloads_path, Path):
			candidates.append(downloads_path.expanduser())
		elif isinstance(downloads_path, str) and downloads_path.strip():
			candidates.append(Path(downloads_path).expanduser())
		if self._applescript_download_dir:
			candidates.append(Path(self._applescript_download_dir).expanduser())
		candidates.append(Path.home() / 'Downloads')

		seen: set[str] = set()
		result: list[Path] = []
		for candidate in candidates:
			resolved = candidate.resolve(strict=False)
			key = str(resolved)
			if key in seen:
				continue
			seen.add(key)
			result.append(resolved)
		return result

	def _snapshot_download_files_sync(self, directories: list[Path]) -> dict[str, tuple[int, float]]:
		snapshot: dict[str, tuple[int, float]] = {}
		temp_suffixes = ('.download', '.crdownload', '.part', '.tmp')
		for directory in directories:
			try:
				if not directory.exists() or not directory.is_dir():
					continue
			except OSError:
				continue

			for path in directory.iterdir():
				try:
					if not path.is_file():
						continue
					name_lower = path.name.lower()
					if name_lower.endswith(temp_suffixes):
						continue
					if name_lower == '.ds_store':
						continue
					stat = path.stat()
					snapshot[str(path.resolve())] = (int(stat.st_size), float(stat.st_mtime))
				except OSError:
					continue
		return snapshot

	async def _snapshot_download_files(self) -> dict[str, tuple[int, float]]:
		return await asyncio.to_thread(self._snapshot_download_files_sync, self._download_directories())

	def _stat_file_sync(self, path: str) -> tuple[bool, int, float]:
		file_path = Path(path).expanduser().resolve(strict=False)
		try:
			if not file_path.is_file():
				return False, 0, 0.0
			stat = file_path.stat()
		except OSError:
			return False, 0, 0.0
		return True, int(stat.st_size), float(stat.st_mtime)

	async def _track_download_path(self, path: str) -> None:
		normalized = _normalize_storage_path(path)
		if normalized in self._downloaded_files:
			return
		self._downloaded_files.append(normalized)

		file_path = Path(normalized)
		self._append_recent_event(f'download:{file_path.name}')

		file_size = 0
		try:
			file_size = int((await asyncio.to_thread(file_path.stat)).st_size)
		except OSError:
			file_size = 0

		current_url = ''
		try:
			current_url = cast(
				str,
				await self._with_retry('download:get_url', self.driver.get_url, retries=1, check_driver_alive=False),
			)
		except Exception:
			current_url = ''

		file_type = file_path.suffix.lower().lstrip('.') or None
		try:
			self.event_bus.dispatch(
				FileDownloadedEvent(
					url=current_url,
					path=normalized,
					file_name=file_path.name,
					file_size=file_size,
					file_type=file_type,
					mime_type=None,
				)
			)
		except Exception:
			return

	async def _sync_from_applescript_recent_downloads(
		self,
		snapshot: dict[str, tuple[int, float]],
		limit: int = 25,
	) -> list[str]:
		"""Best-effort fallback: use AppleScript-reported recent downloads."""
		try:
			entries: list[SafariDownloadEntry] = await safari_list_recent_downloads(limit=limit, timeout_seconds=2.5)
		except Exception as exc:
			self.logger.debug(f'AppleScript recent-download sync unavailable: {exc}')
			return []

		new_paths: list[str] = []
		for entry in entries:
			normalized = _normalize_storage_path(entry.path)
			if normalized in snapshot:
				continue
			exists, size, mtime = await asyncio.to_thread(self._stat_file_sync, normalized)
			if not exists:
				continue
			snapshot[normalized] = (size, mtime)
			new_paths.append(normalized)
		return new_paths

	async def _refresh_download_tracking(self, wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		if self._download_snapshot is None:
			self._download_snapshot = await self._snapshot_download_files()
			return []

		loop = asyncio.get_running_loop()
		deadline = loop.time() + max(timeout_seconds, 0.0)
		current_snapshot = self._download_snapshot
		new_paths: list[str] = []

		while True:
			current_snapshot = await self._snapshot_download_files()
			assert self._download_snapshot is not None
			new_paths = [path for path in current_snapshot if path not in self._download_snapshot]
			if not new_paths and wait_for_new:
				try:
					await safari_show_downloads_ui(timeout_seconds=1.5)
				except Exception:
					pass
				fallback_paths = await self._sync_from_applescript_recent_downloads(current_snapshot)
				if fallback_paths:
					new_paths.extend(fallback_paths)
			if new_paths:
				break
			if not wait_for_new or loop.time() >= deadline:
				break
			await asyncio.sleep(0.2)

		self._download_snapshot = current_snapshot
		for path in sorted(new_paths):
			await self._track_download_path(path)
		return new_paths

	async def _get_applescript_tabs(self, max_age_seconds: float = 1.0) -> list[SafariTabEntry]:
		loop = asyncio.get_running_loop()
		now = loop.time()
		if self._applescript_tabs_cache and now - self._applescript_tabs_cached_at <= max_age_seconds:
			return list(self._applescript_tabs_cache)

		try:
			tabs = await safari_get_tabs(timeout_seconds=1.5)
		except Exception as exc:
			self.logger.debug(f'AppleScript tab sync unavailable: {exc}')
			self._applescript_tabs_cache = []
			self._applescript_tabs_cached_at = now
			return []

		self._applescript_tabs_cache = tabs
		self._applescript_tabs_cached_at = now
		return list(tabs)

	async def _refresh_tabs(self) -> list[TabInfo]:
		async with self._tab_lock:
			tabs = cast(
				list[Any],
				await self._with_retry('tabs:list_tabs', self.driver.list_tabs, retries=1, check_driver_alive=False),
			)
			active_handle = cast(
				str,
				await self._with_retry(
					'tabs:get_window_handle',
					self.driver.get_window_handle,
					retries=1,
					check_driver_alive=False,
				),
			)
			applescript_tabs = await self._get_applescript_tabs()

			target_to_handle: dict[str, str] = {}
			handle_to_target: dict[str, str] = {}
			tab_infos: list[TabInfo] = []

			for tab in tabs:
				target_id = self._handle_to_target.get(tab.handle) or uuid7str()
				tab_title = tab.title or ''
				tab_url = tab.url
				if tab.index < len(applescript_tabs):
					applescript_tab = applescript_tabs[tab.index]
					applescript_title = applescript_tab.title.strip()
					applescript_url = applescript_tab.url.strip()
					if applescript_title and applescript_title.lower() != 'missing value':
						tab_title = applescript_title
					if applescript_url and applescript_url.lower() != 'missing value':
						tab_url = applescript_url

				target_to_handle[target_id] = tab.handle
				handle_to_target[tab.handle] = target_id
				tab_infos.append(TabInfo(target_id=target_id, url=tab_url, title=tab_title, parent_target_id=None))

			self._target_to_handle = target_to_handle
			self._handle_to_target = handle_to_target
			self._tabs_cache = tab_infos

			if active_handle in self._handle_to_target:
				self.agent_focus_target_id = self._handle_to_target[active_handle]
			elif tab_infos:
				self.agent_focus_target_id = tab_infos[-1].target_id
			else:
				self.agent_focus_target_id = None

			return list(tab_infos)

	def _node_text_for_matching(self, node: EnhancedDOMTreeNode) -> str:
		if node.attributes.get('aria-label'):
			return node.attributes.get('aria-label', '')
		if node.attributes.get('value'):
			return node.attributes.get('value', '')
		for child in node.children_nodes or []:
			if child.node_type == NodeType.TEXT_NODE and child.node_value.strip():
				return child.node_value.strip()
		return ''

	def _node_signature(self, node: EnhancedDOMTreeNode) -> tuple[str, str, str, str, str]:
		attrs = node.attributes or {}
		return (
			node.tag_name.lower(),
			attrs.get('id', ''),
			attrs.get('name', ''),
			attrs.get('href', ''),
			self._node_text_for_matching(node)[:80],
		)

	def _ref_signature(self, ref: _SafariElementRef) -> tuple[str, str, str, str, str]:
		attrs = ref.attributes
		return (
			ref.tag_name.lower(),
			attrs.get('id', ''),
			attrs.get('name', ''),
			attrs.get('href', ''),
			(ref.text_content or '')[:80],
		)

	def _distance(self, rect_a: DOMRect | None, rect_b: DOMRect | None) -> float:
		if rect_a is None or rect_b is None:
			return float('inf')
		ax = rect_a.x + (rect_a.width / 2)
		ay = rect_a.y + (rect_a.height / 2)
		bx = rect_b.x + (rect_b.width / 2)
		by = rect_b.y + (rect_b.height / 2)
		return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

	def _tab_index_for_target_id(self, target_id: str) -> int | None:
		for idx, tab in enumerate(self._tabs_cache):
			if tab.target_id == target_id:
				return idx
		return None

	async def _switch_tab_with_applescript_fallback(self, target_id: str) -> bool:
		tab_index = self._tab_index_for_target_id(target_id)
		if tab_index is None:
			return False
		try:
			switched = await safari_switch_tab(tab_index, timeout_seconds=2.5)
		except Exception as exc:
			self.logger.debug(f'AppleScript switch-tab fallback failed: {exc}')
			return False
		if not switched:
			return False

		await asyncio.sleep(0.2)
		await self._refresh_tabs()
		self.agent_focus_target_id = target_id
		self._invalidate_dom_cache()
		self._append_recent_event(f'switch_tab_fallback:{target_id[-4:]}')
		return True

	async def _close_tab_with_applescript_fallback(self, target_id: str) -> bool:
		tab_index = self._tab_index_for_target_id(target_id)
		if tab_index is None:
			return False
		try:
			closed = await safari_close_tab(tab_index, timeout_seconds=2.5)
		except Exception as exc:
			self.logger.debug(f'AppleScript close-tab fallback failed: {exc}')
			return False
		if not closed:
			return False

		await asyncio.sleep(0.2)
		await self._refresh_tabs()
		self._invalidate_dom_cache()
		self._append_recent_event(f'close_tab_fallback:{target_id[-4:]}')
		return True

	async def _rebuild_interactive_dom_state(self) -> SerializedDOMState:
		async with self._state_lock:
			extraction = await extract_interactive_elements(self.driver)
			target_id = self.agent_focus_target_id or 'safari-target'

			document_node = EnhancedDOMTreeNode(
				node_id=0,
				backend_node_id=0,
				node_type=NodeType.DOCUMENT_NODE,
				node_name='#document',
				node_value='',
				attributes={},
				is_scrollable=False,
				is_visible=True,
				absolute_position=None,
				target_id=target_id,
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

			root_simplified = SimplifiedNode(original_node=document_node, children=[], should_display=False, is_interactive=False)
			selector_map: dict[int, EnhancedDOMTreeNode] = {}
			ref_map: dict[int, _SafariElementRef] = {}

			for element in extraction.elements:
				node = self._make_interactive_node(target_id=target_id, element=element, parent=document_node)
				document_children = document_node.children_nodes or []
				document_children.append(node)
				document_node.children_nodes = document_children
				selector_map[element.backend_node_id] = node
				ref_map[element.backend_node_id] = _SafariElementRef(
					backend_node_id=element.backend_node_id,
					stable_id=element.stable_id,
					tag_name=element.tag_name,
					text_content=element.text_content or '',
					attributes=dict(element.attributes),
					css_selector=element.css_selector,
					xpath=element.xpath,
					absolute_position=node.absolute_position,
				)

				child_nodes: list[SimplifiedNode] = []
				for child in node.children_nodes or []:
					child_nodes.append(SimplifiedNode(original_node=child, children=[], is_interactive=False))
				root_simplified.children.append(
					SimplifiedNode(original_node=node, children=child_nodes, is_interactive=True, should_display=True)
				)

			self._cached_selector_map = selector_map
			self._ref_by_backend_id = ref_map
			return SerializedDOMState(_root=root_simplified, selector_map=selector_map)

	def _make_interactive_node(
		self,
		target_id: str,
		element: SafariExtractedElement,
		parent: EnhancedDOMTreeNode,
	) -> EnhancedDOMTreeNode:
		attributes = dict(element.attributes)
		attributes.setdefault(_STABLE_ID_ATTR, element.stable_id)
		text_content = (element.text_content or '').strip()
		if text_content and 'aria-label' not in attributes and element.tag_name not in {'input', 'textarea'}:
			attributes['aria-label'] = text_content[:120]

		rect = DOMRect(
			x=float(element.bounding_rect.x),
			y=float(element.bounding_rect.y),
			width=float(element.bounding_rect.width),
			height=float(element.bounding_rect.height),
		)
		snapshot = EnhancedSnapshotNode(
			is_clickable=True,
			cursor_style='pointer',
			bounds=rect,
			clientRects=rect,
			scrollRects=None,
			computed_styles=None,
			paint_order=None,
			stacking_contexts=None,
		)

		node = EnhancedDOMTreeNode(
			node_id=element.backend_node_id,
			backend_node_id=element.backend_node_id,
			node_type=NodeType.ELEMENT_NODE,
			node_name=element.tag_name.upper(),
			node_value='',
			attributes=attributes,
			is_scrollable=element.is_scrollable,
			is_visible=element.is_visible,
			absolute_position=rect,
			target_id=target_id,
			frame_id='main',
			session_id='safari',
			content_document=None,
			shadow_root_type=None,
			shadow_roots=[],
			parent_node=parent,
			children_nodes=[],
			ax_node=None,
			snapshot_node=snapshot,
		)

		if text_content:
			text_node = EnhancedDOMTreeNode(
				node_id=(element.backend_node_id * 1000) + 1,
				backend_node_id=(element.backend_node_id * 1000) + 1,
				node_type=NodeType.TEXT_NODE,
				node_name='#text',
				node_value=text_content,
				attributes={},
				is_scrollable=False,
				is_visible=element.is_visible,
				absolute_position=rect,
				target_id=target_id,
				frame_id='main',
				session_id='safari',
				content_document=None,
				shadow_root_type=None,
				shadow_roots=[],
				parent_node=node,
				children_nodes=[],
				ax_node=None,
				snapshot_node=EnhancedSnapshotNode(
					is_clickable=False,
					cursor_style=None,
					bounds=rect,
					clientRects=rect,
					scrollRects=None,
					computed_styles=None,
					paint_order=None,
					stacking_contexts=None,
				),
			)
			children_nodes = node.children_nodes or []
			children_nodes.append(text_node)
			node.children_nodes = children_nodes

		return node

	async def _resolve_element_ref(self, node: EnhancedDOMTreeNode) -> _SafariElementRef:
		if node.backend_node_id in self._ref_by_backend_id:
			return self._ref_by_backend_id[node.backend_node_id]

		await self._rebuild_interactive_dom_state()
		if node.backend_node_id in self._ref_by_backend_id:
			return self._ref_by_backend_id[node.backend_node_id]

		stable_id = str(node.attributes.get(_STABLE_ID_ATTR, '')).strip()
		if stable_id:
			for ref in self._ref_by_backend_id.values():
				if ref.stable_id == stable_id:
					return ref

		signature = self._node_signature(node)
		candidates = [ref for ref in self._ref_by_backend_id.values() if self._ref_signature(ref) == signature]
		if not candidates:
			raise BrowserError(
				message=f'Element index {node.backend_node_id} no longer exists',
				long_term_memory='Element changed after page update. Refresh browser state and retry the action.',
			)

		best_ref = min(candidates, key=lambda ref: self._distance(node.absolute_position, ref.absolute_position))
		return best_ref

	async def _scroll_element_into_view(self, ref: _SafariElementRef) -> dict[str, Any]:
		return cast(
			dict[str, Any],
			await self._with_retry(
				'scroll_into_view:execute_js',
				lambda: self.driver.execute_js(
					"""
					function resolveElement(cssSelector, xpathSelector) {
						let element = null;
						if (cssSelector) {
							try { element = document.querySelector(cssSelector); } catch (_) {}
						}
						if (!element && xpathSelector) {
							try {
								element = document.evaluate(
									xpathSelector,
									document,
									null,
									XPathResult.FIRST_ORDERED_NODE_TYPE,
									null
								).singleNodeValue;
							} catch (_) {}
						}
						return element;
					}

					const element = resolveElement(arguments[0], arguments[1]);
					if (!element) {
						return { ok: false, error: 'Element not found' };
					}

					element.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
					const rect = element.getBoundingClientRect();
					return {
						ok: true,
						tag: element.tagName ? element.tagName.toLowerCase() : '',
						rect: {
							x: rect.x,
							y: rect.y,
							width: rect.width,
							height: rect.height,
						},
					};
					""",
					ref.css_selector,
					ref.xpath,
				),
				retries=1,
			),
		)

	def _center_from_rect(self, rect: dict[str, Any]) -> tuple[float, float]:
		x = float(rect.get('x', 0.0)) + (float(rect.get('width', 0.0)) / 2)
		y = float(rect.get('y', 0.0)) + (float(rect.get('height', 0.0)) / 2)
		return x, y

	def _rect_dict_from_dom_rect(self, rect: DOMRect | None) -> dict[str, float] | None:
		if rect is None:
			return None
		return {
			'x': float(rect.x),
			'y': float(rect.y),
			'width': float(rect.width),
			'height': float(rect.height),
		}

	async def _compute_page_info(self) -> PageInfo:
		layout = await self._with_retry(
			'page_info:execute_js',
			lambda: self.driver.execute_js(
				"""
				return {
					viewport_width: window.innerWidth || document.documentElement.clientWidth || 0,
					viewport_height: window.innerHeight || document.documentElement.clientHeight || 0,
					page_width: Math.max(
						document.body ? document.body.scrollWidth : 0,
						document.documentElement ? document.documentElement.scrollWidth : 0,
						window.innerWidth || 0
					),
					page_height: Math.max(
						document.body ? document.body.scrollHeight : 0,
						document.documentElement ? document.documentElement.scrollHeight : 0,
						window.innerHeight || 0
					),
					scroll_x: window.scrollX || window.pageXOffset || document.documentElement.scrollLeft || 0,
					scroll_y: window.scrollY || window.pageYOffset || document.documentElement.scrollTop || 0
				};
				"""
			),
			retries=1,
			check_driver_alive=False,
		)
		if not isinstance(layout, dict):
			layout = {}

		viewport_width = int(layout.get('viewport_width', 0) or 0)
		viewport_height = int(layout.get('viewport_height', 0) or 0)
		page_width = int(layout.get('page_width', viewport_width) or viewport_width)
		page_height = int(layout.get('page_height', viewport_height) or viewport_height)
		scroll_x = int(layout.get('scroll_x', 0) or 0)
		scroll_y = int(layout.get('scroll_y', 0) or 0)

		pixels_above = max(scroll_y, 0)
		pixels_left = max(scroll_x, 0)
		pixels_below = max(page_height - (scroll_y + viewport_height), 0)
		pixels_right = max(page_width - (scroll_x + viewport_width), 0)

		self._original_viewport_size = (viewport_width, viewport_height)

		return PageInfo(
			viewport_width=viewport_width,
			viewport_height=viewport_height,
			page_width=page_width,
			page_height=page_height,
			scroll_x=scroll_x,
			scroll_y=scroll_y,
			pixels_above=pixels_above,
			pixels_below=pixels_below,
			pixels_left=pixels_left,
			pixels_right=pixels_right,
		)

	async def _build_text_dom_tree_for_markdown(self) -> EnhancedDOMTreeNode:
		target_id = self.agent_focus_target_id or 'safari-target'
		page_text = await self._with_retry(
			'text_dom:execute_js',
			lambda: self.driver.execute_js('return document.body ? document.body.innerText : "";'),
			retries=1,
			check_driver_alive=False,
		)
		if not isinstance(page_text, str):
			page_text = str(page_text)

		document_node = EnhancedDOMTreeNode(
			node_id=100000,
			backend_node_id=100000,
			node_type=NodeType.DOCUMENT_NODE,
			node_name='#document',
			node_value='',
			attributes={},
			is_scrollable=False,
			is_visible=True,
			absolute_position=None,
			target_id=target_id,
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

		html_node = EnhancedDOMTreeNode(
			node_id=100001,
			backend_node_id=100001,
			node_type=NodeType.ELEMENT_NODE,
			node_name='HTML',
			node_value='',
			attributes={},
			is_scrollable=False,
			is_visible=True,
			absolute_position=None,
			target_id=target_id,
			frame_id='main',
			session_id='safari',
			content_document=None,
			shadow_root_type=None,
			shadow_roots=[],
			parent_node=document_node,
			children_nodes=[],
			ax_node=None,
			snapshot_node=None,
		)

		body_node = EnhancedDOMTreeNode(
			node_id=100002,
			backend_node_id=100002,
			node_type=NodeType.ELEMENT_NODE,
			node_name='BODY',
			node_value='',
			attributes={},
			is_scrollable=True,
			is_visible=True,
			absolute_position=None,
			target_id=target_id,
			frame_id='main',
			session_id='safari',
			content_document=None,
			shadow_root_type=None,
			shadow_roots=[],
			parent_node=html_node,
			children_nodes=[],
			ax_node=None,
			snapshot_node=None,
		)

		text_node = EnhancedDOMTreeNode(
			node_id=100003,
			backend_node_id=100003,
			node_type=NodeType.TEXT_NODE,
			node_name='#text',
			node_value=page_text,
			attributes={},
			is_scrollable=False,
			is_visible=True,
			absolute_position=None,
			target_id=target_id,
			frame_id='main',
			session_id='safari',
			content_document=None,
			shadow_root_type=None,
			shadow_roots=[],
			parent_node=body_node,
			children_nodes=[],
			ax_node=None,
			snapshot_node=None,
		)

		body_children = body_node.children_nodes or []
		body_children.append(text_node)
		body_node.children_nodes = body_children

		html_children = html_node.children_nodes or []
		html_children.append(body_node)
		html_node.children_nodes = html_children

		document_children = document_node.children_nodes or []
		document_children.append(html_node)
		document_node.children_nodes = document_children
		return document_node

	@validate_call
	async def start(self) -> None:
		if self._started:
			return
		profile_activation_error: Exception | None = None
		driver_start_error: Exception | None = None
		if self.safari_profile_name:
			try:
				selected_item = await safari_open_profile_window(self.safari_profile_name, timeout_seconds=6.0)
				self._append_recent_event(f'profile:{selected_item}')
			except Exception as exc:
				profile_activation_error = exc
				self.logger.warning(f'Failed to activate Safari profile "{self.safari_profile_name}": {exc}')
				self._append_recent_event(f'profile:failed:{self.safari_profile_name}')
		if self.safari_profile_name:
			start_timeout = min(float(self.driver.config.command_timeout), 20.0)
			try:
				await self._start_driver_with_pairing_recovery(timeout_seconds=start_timeout)
			except Exception as exc:
				driver_start_error = exc
				self.logger.warning(
					f'Safari WebDriver startup after profile activation failed: {exc}. Retrying with default Safari context'
				)
				self.driver._driver = None
				await self._start_driver_with_pairing_recovery(timeout_seconds=start_timeout)
		else:
			await self._start_driver_with_pairing_recovery(timeout_seconds=float(self.driver.config.command_timeout))
		if not await self.driver.is_alive():
			if self.safari_profile_name:
				self.logger.warning(
					'Safari WebDriver is not alive after profile activation; retrying with default Safari context'
				)
				retry_timeout = min(float(self.driver.config.command_timeout), 15.0)
				original_timeout = float(self.driver.config.command_timeout)
				self.driver.config.command_timeout = retry_timeout
				try:
					try:
						await asyncio.wait_for(self.driver.close(), timeout=retry_timeout)
					except Exception:
						# If close hangs or fails, clear local handle and force a fresh start attempt.
						self.driver._driver = None
					if self.driver._driver is not None:
						self.driver._driver = None
					await self._start_driver_with_pairing_recovery(timeout_seconds=retry_timeout)
				finally:
					self.driver.config.command_timeout = original_timeout
			if not await self.driver.is_alive():
				profile_suffix = f' (requested profile: {self.safari_profile_name})' if self.safari_profile_name else ''
				raise RuntimeError(f'Safari WebDriver session is not alive after startup{profile_suffix}')
		self._dom_watchdog = _SafariDOMWatchdogShim(self)
		self._cdp_shim = _CDPClientShim(self)
		self._cdp_client_root = self._cdp_shim
		self.cdp_url = 'safari://webdriver'
		self._register_handlers()
		await self._refresh_tabs()
		await self._refresh_applescript_download_dir()
		self._download_snapshot = await self._snapshot_download_files()
		await self.load_storage_state()
		self._started = True
		if profile_activation_error is not None:
			self.logger.warning(
				f'Safari session started without profile switch "{self.safari_profile_name}"; default context is active'
			)
		if driver_start_error is not None:
			self.logger.warning(
				f'Safari session recovered after profile startup failure "{self.safari_profile_name}"; default context is active'
			)

	@validate_call
	async def stop(self) -> None:
		if not self._started and not self.driver.is_started:
			return
		try:
			await self.save_storage_state()
		except Exception as exc:
			self.logger.debug(f'Failed to save Safari storage state: {exc}')
		await self.driver.close()
		self._started = False
		self._cdp_client_root = None
		self._cached_browser_state_summary = None
		self._cached_selector_map.clear()
		self._ref_by_backend_id.clear()
		self._tabs_cache = []
		self._target_to_handle.clear()
		self._handle_to_target.clear()
		self._download_snapshot = None
		self._applescript_download_dir = None
		self._applescript_download_dir_checked = False
		self._applescript_tabs_cache.clear()
		self._applescript_tabs_cached_at = 0.0

	async def close(self) -> None:
		await self.stop()

	async def kill(self) -> None:
		await self.stop()

	async def is_alive(self) -> bool:
		return await self.driver.is_alive()

	async def navigate(self, url: str) -> str:
		await self.start()
		final_url = await self._with_retry('navigate', lambda: self.driver.navigate(url))
		await self._dismiss_dialog_if_any()
		await self._refresh_tabs()
		self._invalidate_dom_cache()
		return str(final_url)

	async def screenshot(self) -> str:
		await self.start()
		return cast(str, await self._with_retry('screenshot', self.driver.screenshot))

	async def get_dom(self, max_elements: int = 400) -> SafariDOMExtractionResult:
		await self.start()
		return await extract_interactive_elements(self.driver, max_elements=max_elements)

	async def cookies(self) -> list[dict[str, Any]]:
		await self.start()
		return cast(list[dict[str, Any]], await self._with_retry('cookies:get_cookies', self.driver.get_cookies, retries=1))

	async def get_tabs(self) -> list[TabInfo]:
		await self.start()
		return await self._refresh_tabs()

	async def get_current_page_url(self) -> str:
		await self.start()
		return cast(str, await self._with_retry('page:get_url', self.driver.get_url, retries=1))

	async def get_current_page_title(self) -> str:
		await self.start()
		return cast(str, await self._with_retry('page:get_title', self.driver.get_title, retries=1))

	async def get_target_id_from_tab_id(self, tab_id: str) -> str:
		await self.start()
		await self._refresh_tabs()
		for target_id in self._target_to_handle:
			if target_id.endswith(tab_id):
				return target_id
		raise ValueError(f'No tab found with tab_id suffix: {tab_id}')

	async def get_selector_map(self) -> dict[int, EnhancedDOMTreeNode]:
		if self._cached_selector_map:
			return self._cached_selector_map
		state = await self.get_browser_state_summary(include_screenshot=False)
		return state.dom_state.selector_map

	async def get_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		selector_map = await self.get_selector_map()
		return selector_map.get(index)

	async def get_dom_element_by_index(self, index: int) -> EnhancedDOMTreeNode | None:
		return await self.get_element_by_index(index)

	def update_cached_selector_map(self, selector_map: dict[int, EnhancedDOMTreeNode]) -> None:
		self._cached_selector_map = dict(selector_map)

	async def get_index_by_id(self, element_id: str) -> int | None:
		selector_map = await self.get_selector_map()
		for idx, element in selector_map.items():
			if element.attributes.get('id') == element_id:
				return idx
		return None

	async def get_index_by_class(self, class_name: str) -> int | None:
		selector_map = await self.get_selector_map()
		for idx, element in selector_map.items():
			class_attr = element.attributes.get('class', '')
			if class_name in class_attr.split():
				return idx
		return None

	async def get_most_recently_opened_target_id(self) -> str:
		tabs = await self.get_tabs()
		if not tabs:
			raise RuntimeError('No tabs available')
		return tabs[-1].target_id

	def is_file_input(self, element: Any) -> bool:
		if not isinstance(element, EnhancedDOMTreeNode):
			return False
		return element.node_name.upper() == 'INPUT' and element.attributes.get('type', '').lower() == 'file'

	async def get_browser_state_summary(
		self,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		if cached and self._cached_browser_state_summary is not None:
			if include_screenshot and not self._cached_browser_state_summary.screenshot:
				pass
			else:
				return self._cached_browser_state_summary

		event = cast(
			BrowserStateRequestEvent,
			self.event_bus.dispatch(
				BrowserStateRequestEvent(
					include_dom=True,
					include_screenshot=include_screenshot,
					include_recent_events=include_recent_events,
				)
			),
		)
		result = await event.event_result(raise_if_none=True, raise_if_any=True)
		assert result is not None
		return result

	async def get_state_as_text(self) -> str:
		state = await self.get_browser_state_summary(include_screenshot=True)
		return state.dom_state.llm_representation()

	async def take_screenshot(
		self,
		path: str | None = None,
		full_page: bool = False,
		format: str = 'png',
		quality: int | None = None,
		clip: dict | None = None,
	) -> bytes:
		del full_page, format, quality, clip
		screenshot_b64 = await self.screenshot()
		screenshot_bytes = base64.b64decode(screenshot_b64)
		if path:
			await anyio.Path(path).write_bytes(screenshot_bytes)
		return screenshot_bytes

	async def get_or_create_cdp_session(self, target_id: str | None = None, focus: bool = True) -> _CDPSessionShim:
		await self.start()
		if target_id is not None and focus:
			handle = self._target_to_handle.get(target_id)
			if handle:
				await self._with_retry(
					'cdp_session:switch_to_handle',
					lambda: self.driver.switch_to_handle(handle),
					retries=1,
					check_driver_alive=False,
				)
				self.agent_focus_target_id = target_id
		else:
			await self._refresh_tabs()
		current_target = self.agent_focus_target_id or target_id or 'safari-target'
		assert self._cdp_shim is not None
		return _CDPSessionShim(cdp_client=self._cdp_shim, target_id=current_target)

	async def send_demo_mode_log(self, message: str, level: str = 'info', metadata: dict[str, Any] | None = None) -> None:
		del message, level, metadata
		return None

	async def highlight_interaction_element(self, node: EnhancedDOMTreeNode) -> None:
		del node
		return None

	async def highlight_coordinate_click(self, x: int, y: int) -> None:
		del x, y
		return None

	async def remove_highlights(self) -> None:
		return None

	async def save_storage_state(self, path: str | None = None) -> None:
		if not self.driver.is_started:
			return
		storage_state = self.browser_profile.storage_state
		if path is None:
			if isinstance(storage_state, (str, Path)):
				path = str(storage_state)
			else:
				return

		if not path:
			return

		cookies = cast(
			list[dict[str, Any]],
			await self._with_retry(
				'save_storage_state:get_cookies',
				self.driver.get_cookies,
				retries=1,
				check_driver_alive=False,
			),
		)
		storage = await self._with_retry(
			'save_storage_state:execute_js',
			lambda: self.driver.execute_js(
				"""
				const local = {};
				for (let i = 0; i < localStorage.length; i++) {
					const key = localStorage.key(i);
					if (key != null) local[key] = localStorage.getItem(key);
				}
				const session = {};
				for (let i = 0; i < sessionStorage.length; i++) {
					const key = sessionStorage.key(i);
					if (key != null) session[key] = sessionStorage.getItem(key);
				}
				return { localStorage: local, sessionStorage: session };
				"""
			),
			retries=1,
			check_driver_alive=False,
		)
		if not isinstance(storage, dict):
			storage = {'localStorage': {}, 'sessionStorage': {}}

		state = {
			'cookies': cookies,
			'origins': [],
			'localStorage': storage.get('localStorage', {}),
			'sessionStorage': storage.get('sessionStorage', {}),
		}
		normalized_path = _normalize_storage_path(path)
		output_path = anyio.Path(normalized_path)
		await output_path.write_text(json.dumps(state, indent=2), encoding='utf-8')

	async def export_storage_state(self, output_path: str | Path | None = None) -> dict[str, Any]:
		await self.start()
		cookies = cast(
			list[dict[str, Any]],
			await self._with_retry(
				'export_storage_state:get_cookies',
				self.driver.get_cookies,
				retries=1,
				check_driver_alive=False,
			),
		)
		storage = await self._with_retry(
			'export_storage_state:execute_js',
			lambda: self.driver.execute_js(
				"""
				const local = {};
				for (let i = 0; i < localStorage.length; i++) {
					const key = localStorage.key(i);
					if (key != null) local[key] = localStorage.getItem(key);
				}
				return local;
				"""
			),
			retries=1,
			check_driver_alive=False,
		)
		storage_state = {
			'cookies': cookies,
			'origins': [],
			'localStorage': storage if isinstance(storage, dict) else {},
		}
		if output_path is not None:
			normalized_path = _normalize_storage_path(output_path)
			await anyio.Path(normalized_path).write_text(json.dumps(storage_state, indent=2), encoding='utf-8')
		return storage_state

	async def load_storage_state(self) -> None:
		if not self.driver.is_started:
			return
		storage_state = self.browser_profile.storage_state

		state_data: dict[str, Any] | None = None
		if isinstance(storage_state, dict):
			state_data = storage_state
		elif isinstance(storage_state, (str, Path)):
			normalized_path = _normalize_storage_path(storage_state)
			storage_path = anyio.Path(normalized_path)
			if await storage_path.exists():
				state_data = json.loads(await storage_path.read_text(encoding='utf-8'))

		if not state_data:
			return

		cookies = state_data.get('cookies', []) if isinstance(state_data, dict) else []
		if isinstance(cookies, list):
			cookies_by_domain: dict[str, list[dict[str, Any]]] = {}
			for cookie in cookies:
				if not isinstance(cookie, dict):
					continue
				domain = str(cookie.get('domain') or '')
				domain = domain.lstrip('.')
				if not domain:
					continue
				cookies_by_domain.setdefault(domain, []).append(cookie)

			for domain, cookie_items in cookies_by_domain.items():
				try:
					await self._with_retry(
						'load_storage_state:navigate_domain',
						lambda domain=domain: self.driver.navigate(f'https://{domain}'),
						retries=1,
						check_driver_alive=False,
					)
				except Exception:
					continue
				for cookie in cookie_items:
					try:
						await self._with_retry(
							'load_storage_state:set_cookie',
							lambda cookie=cookie: self.driver.set_cookie(cookie),
							retries=1,
							check_driver_alive=False,
						)
					except Exception:
						continue

		local_storage = state_data.get('localStorage', {}) if isinstance(state_data, dict) else {}
		session_storage = state_data.get('sessionStorage', {}) if isinstance(state_data, dict) else {}
		if isinstance(local_storage, dict) or isinstance(session_storage, dict):
			await self._with_retry(
				'load_storage_state:execute_js',
				lambda: self.driver.execute_js(
					"""
					const localStorageData = arguments[0] || {};
					const sessionStorageData = arguments[1] || {};
					for (const [key, value] of Object.entries(localStorageData)) {
						try { localStorage.setItem(key, String(value)); } catch (_) {}
					}
					for (const [key, value] of Object.entries(sessionStorageData)) {
						try { sessionStorage.setItem(key, String(value)); } catch (_) {}
					}
					return true;
					""",
					local_storage if isinstance(local_storage, dict) else {},
					session_storage if isinstance(session_storage, dict) else {},
				),
				retries=1,
				check_driver_alive=False,
			)

		self._invalidate_dom_cache()

	async def clear_cookies(self) -> None:
		await self.start()
		await self._with_retry('clear_cookies', self.driver.clear_cookies, retries=1, check_driver_alive=False)
		self._invalidate_dom_cache()

	async def on_FileDownloadedEvent(self, event: FileDownloadedEvent) -> None:
		normalized_path = _normalize_storage_path(event.path)
		if normalized_path not in self._downloaded_files:
			self._downloaded_files.append(normalized_path)
			self._append_recent_event(f'download:{Path(normalized_path).name}')

	# ------------------------------------------------------------------
	# Event handlers
	# ------------------------------------------------------------------

	async def on_NavigateToUrlEvent(self, event: NavigateToUrlEvent) -> None:
		await self.start()

		async def _navigate() -> None:
			await self._refresh_download_tracking(wait_for_new=False)
			if event.new_tab:
				try:
					await self.driver.new_tab(event.url)
				except Exception:
					await safari_open_tab(event.url, timeout_seconds=2.5)
					await asyncio.sleep(0.2)
			else:
				await self.driver.navigate(event.url)
			await self.driver.wait_for_ready_state(timeout_seconds=12.0)
			await self._dismiss_dialogs_after_navigation(max_wait_seconds=0.8, poll_interval_seconds=0.15)
			await self._refresh_tabs()
			await self._refresh_download_tracking(wait_for_new=True, timeout_seconds=1.0)
			self._invalidate_dom_cache()
			self._append_recent_event(f'navigate:{event.url}')

		await self._with_retry('NavigateToUrlEvent', _navigate)

	async def on_ClickElementEvent(self, event: ClickElementEvent) -> dict[str, Any] | None:
		await self.start()

		async def _click() -> dict[str, Any] | None:
			await self._refresh_download_tracking(wait_for_new=False)
			ref = await self._resolve_element_ref(event.node)
			location = await self._scroll_element_into_view(ref)
			rect: dict[str, Any] | None = None
			if isinstance(location, dict) and location.get('ok'):
				rect = cast(dict[str, Any], location.get('rect', {}))
			else:
				rect = self._rect_dict_from_dom_rect(ref.absolute_position)
				if rect is None or float(rect.get('width', 0.0)) <= 0 or float(rect.get('height', 0.0)) <= 0:
					raise BrowserError(
						message=f'Element {event.node.backend_node_id} could not be located in DOM',
						long_term_memory='Element changed before click; refresh state and retry.',
					)
				self._append_recent_event(f'fallback:cached-rect-click:{event.node.backend_node_id}')

			x, y = self._center_from_rect(rect)
			await self.driver.click_at(x, y)
			await self._dismiss_dialog_if_any()
			await self._refresh_tabs()
			await self._refresh_download_tracking(wait_for_new=True, timeout_seconds=1.2)
			self._invalidate_dom_cache()
			self._append_recent_event(f'click:{event.node.backend_node_id}')
			return {'x': int(x), 'y': int(y)}

		return cast(dict[str, Any] | None, await self._with_retry('ClickElementEvent', _click))

	async def on_ClickCoordinateEvent(self, event: ClickCoordinateEvent) -> dict[str, Any] | None:
		await self.start()

		async def _click() -> dict[str, Any] | None:
			await self._refresh_download_tracking(wait_for_new=False)
			await self.driver.click_at(event.coordinate_x, event.coordinate_y)
			await self._dismiss_dialog_if_any()
			await self._refresh_tabs()
			await self._refresh_download_tracking(wait_for_new=True, timeout_seconds=1.2)
			self._invalidate_dom_cache()
			self._append_recent_event(f'click@{event.coordinate_x},{event.coordinate_y}')
			return {'x': event.coordinate_x, 'y': event.coordinate_y}

		return cast(dict[str, Any] | None, await self._with_retry('ClickCoordinateEvent', _click))

	async def on_TypeTextEvent(self, event: TypeTextEvent) -> dict[str, Any] | None:
		await self.start()

		async def _type() -> dict[str, Any] | None:
			ref = await self._resolve_element_ref(event.node)
			location = await self._scroll_element_into_view(ref)
			rect: dict[str, Any] | None = None
			if isinstance(location, dict) and location.get('ok'):
				rect = cast(dict[str, Any], location.get('rect', {}))
			else:
				rect = self._rect_dict_from_dom_rect(ref.absolute_position)
				if rect is None or float(rect.get('width', 0.0)) <= 0 or float(rect.get('height', 0.0)) <= 0:
					raise BrowserError(
						message=f'Element {event.node.backend_node_id} could not be focused for typing',
						long_term_memory='Element changed before typing; refresh state and retry.',
					)
				self._append_recent_event(f'fallback:cached-rect-type:{event.node.backend_node_id}')

			x, y = self._center_from_rect(rect)
			await self.driver.click_at(x, y)

			if event.clear:
				await self.driver.execute_js(
					"""
					const active = document.activeElement;
					if (!active) return false;
					if ('value' in active) active.value = '';
					if (active.isContentEditable) active.textContent = '';
					active.dispatchEvent(new Event('input', { bubbles: true }));
					active.dispatchEvent(new Event('change', { bubbles: true }));
					return true;
					"""
				)

			await self.driver.send_keys(event.text)
			actual_value = await self.driver.execute_js(
				"""
				const active = document.activeElement;
				if (!active) return null;
				if ('value' in active) return active.value;
				if (active.isContentEditable) return active.textContent || '';
				return null;
				"""
			)
			await self._dismiss_dialog_if_any()
			await self._refresh_tabs()
			self._invalidate_dom_cache()
			self._append_recent_event(f'type:{event.node.backend_node_id}')
			return {
				'x': int(x),
				'y': int(y),
				'actual_value': actual_value,
			}

		return cast(dict[str, Any] | None, await self._with_retry('TypeTextEvent', _type))

	async def on_ScrollEvent(self, event: ScrollEvent) -> None:
		await self.start()
		direction_multiplier = 1 if event.direction in {'down', 'right'} else -1

		async def _scroll() -> None:
			if event.node is None:
				if event.direction in {'up', 'down'}:
					await self.driver.execute_js('window.scrollBy(0, arguments[0]);', direction_multiplier * event.amount)
				else:
					await self.driver.execute_js('window.scrollBy(arguments[0], 0);', direction_multiplier * event.amount)
			else:
				ref = await self._resolve_element_ref(event.node)
				if event.direction in {'up', 'down'}:
					await self.driver.execute_js(
						"""
						function resolveElement(cssSelector, xpathSelector) {
							let element = null;
							if (cssSelector) {
								try { element = document.querySelector(cssSelector); } catch (_) {}
							}
							if (!element && xpathSelector) {
								try {
									element = document.evaluate(xpathSelector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
								} catch (_) {}
							}
							return element;
						}
						const element = resolveElement(arguments[0], arguments[1]);
						if (!element) return false;
						element.scrollBy(0, arguments[2]);
						return true;
						""",
						ref.css_selector,
						ref.xpath,
						direction_multiplier * event.amount,
					)
				else:
					await self.driver.execute_js(
						"""
						function resolveElement(cssSelector, xpathSelector) {
							let element = null;
							if (cssSelector) {
								try { element = document.querySelector(cssSelector); } catch (_) {}
							}
							if (!element && xpathSelector) {
								try {
									element = document.evaluate(xpathSelector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
								} catch (_) {}
							}
							return element;
						}
						const element = resolveElement(arguments[0], arguments[1]);
						if (!element) return false;
						element.scrollBy(arguments[2], 0);
						return true;
						""",
						ref.css_selector,
						ref.xpath,
						direction_multiplier * event.amount,
					)

			await self._dismiss_dialog_if_any()
			self._invalidate_dom_cache()
			self._append_recent_event(f'scroll:{event.direction}:{event.amount}')

		await self._with_retry('ScrollEvent', _scroll)

	async def on_ScrollToTextEvent(self, event: ScrollToTextEvent) -> None:
		await self.start()

		async def _scroll_to_text() -> None:
			result = await self.driver.execute_js(
				"""
				const targetText = String(arguments[0] || '').toLowerCase();
				const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
				let node = null;
				while ((node = walker.nextNode())) {
					const value = (node.nodeValue || '').toLowerCase();
					if (value.includes(targetText)) {
						if (node.parentElement) {
							node.parentElement.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
							return true;
						}
					}
				}
				return false;
				""",
				event.text,
			)
			if not result:
				raise BrowserError(
					message=f"Text '{event.text}' not found on page",
					long_term_memory=f"Text '{event.text}' was not found while scrolling.",
				)
			self._invalidate_dom_cache()
			self._append_recent_event(f'scroll_to_text:{event.text[:50]}')

		await self._with_retry('ScrollToTextEvent', _scroll_to_text)

	async def on_ScreenshotEvent(self, event: ScreenshotEvent) -> str:
		del event
		await self.start()
		try:
			screenshot_b64 = await self.driver.screenshot()
			return screenshot_b64
		except Exception as exc:
			cached = self._cached_browser_state_summary
			if cached is not None and cached.screenshot:
				self.logger.warning(f'Safari screenshot event failed, using cached screenshot: {type(exc).__name__}: {exc}')
				self._append_recent_event('screenshot_event_fallback:cached')
				return cached.screenshot
			raise BrowserError(
				message=f'Safari screenshot failed: {type(exc).__name__}: {exc}',
				long_term_memory='Screenshot capture failed in Safari. Continue without image or retry the action.',
			) from exc

	async def on_BrowserStateRequestEvent(self, event: BrowserStateRequestEvent) -> BrowserStateSummary:
		await self.start()
		browser_errors: list[str] = []
		cached_summary = self._cached_browser_state_summary

		try:
			await self._refresh_download_tracking(wait_for_new=False)
		except Exception as exc:
			error_text = f'Safari download tracking failed: {type(exc).__name__}: {exc}'
			self.logger.warning(error_text)
			browser_errors.append(error_text)

		url = cached_summary.url if cached_summary is not None else ''
		title = cached_summary.title if cached_summary is not None else ''
		tabs = list(cached_summary.tabs) if cached_summary is not None else []

		try:
			url = cast(
				str,
				await self._with_retry(
					'BrowserStateRequestEvent:get_url',
					self.driver.get_url,
					retries=1,
					check_driver_alive=False,
				),
			)
		except Exception as exc:
			error_text = f'Safari URL read failed: {type(exc).__name__}: {exc}'
			self.logger.warning(error_text)
			browser_errors.append(error_text)

		try:
			title = cast(
				str,
				await self._with_retry(
					'BrowserStateRequestEvent:get_title',
					self.driver.get_title,
					retries=1,
					check_driver_alive=False,
				),
			)
		except Exception as exc:
			error_text = f'Safari title read failed: {type(exc).__name__}: {exc}'
			self.logger.warning(error_text)
			browser_errors.append(error_text)

		try:
			tabs = await self._refresh_tabs()
		except Exception as exc:
			error_text = f'Safari tab refresh failed: {type(exc).__name__}: {exc}'
			self.logger.warning(error_text)
			browser_errors.append(error_text)
			if not tabs:
				fallback_target = self.agent_focus_target_id or 'safari-target'
				fallback_url = url or 'about:blank'
				fallback_title = title or 'Untitled'
				tabs = [TabInfo(url=fallback_url, title=fallback_title, target_id=fallback_target, parent_target_id=None)]

		dom_state = SerializedDOMState(_root=None, selector_map={})
		if event.include_dom:
			try:
				dom_state = await self._rebuild_interactive_dom_state()
			except Exception as exc:
				error_text = f'Safari DOM extraction failed: {type(exc).__name__}: {exc}'
				self.logger.warning(error_text)
				self._append_recent_event('dom_fallback:cached_or_empty')
				browser_errors.append(error_text)
				if self._cached_browser_state_summary is not None:
					dom_state = self._cached_browser_state_summary.dom_state

		screenshot: str | None = None
		if event.include_screenshot:
			try:
				screenshot = cast(
					str,
					await self._with_retry(
						'BrowserStateRequestEvent:screenshot',
						self.driver.screenshot,
						retries=1,
						check_driver_alive=False,
					),
				)
			except Exception as exc:
				error_text = f'Safari screenshot failed: {type(exc).__name__}: {exc}'
				self.logger.warning(error_text)
				browser_errors.append(error_text)
				if cached_summary is not None and cached_summary.screenshot:
					screenshot = cached_summary.screenshot
					self._append_recent_event('screenshot_fallback:cached')

		try:
			page_info = await self._compute_page_info()
		except Exception as exc:
			error_text = f'Safari page metrics failed: {type(exc).__name__}: {exc}'
			self.logger.warning(error_text)
			browser_errors.append(error_text)
			if self._cached_browser_state_summary is not None and self._cached_browser_state_summary.page_info is not None:
				page_info = self._cached_browser_state_summary.page_info
			else:
				page_info = PageInfo(
					viewport_width=0,
					viewport_height=0,
					page_width=0,
					page_height=0,
					scroll_x=0,
					scroll_y=0,
					pixels_above=0,
					pixels_below=0,
					pixels_left=0,
					pixels_right=0,
				)

		summary = BrowserStateSummary(
			dom_state=dom_state,
			url=url,
			title=title,
			tabs=tabs,
			screenshot=screenshot,
			page_info=page_info,
			pixels_above=page_info.pixels_above,
			pixels_below=page_info.pixels_below,
			browser_errors=browser_errors,
			is_pdf_viewer=False,
			recent_events=self._recent_events_text() if event.include_recent_events else None,
			pending_network_requests=[],
			pagination_buttons=[],
			closed_popup_messages=self._closed_popup_messages[-10:],
		)
		self._cached_browser_state_summary = summary
		return summary

	async def on_SwitchTabEvent(self, event: SwitchTabEvent) -> str:
		await self.start()
		await self._refresh_tabs()

		if event.target_id is None:
			if not self._tabs_cache:
				try:
					new_tab = cast(
						Any,
						await self._with_retry(
							'SwitchTabEvent:new_tab',
							lambda: self.driver.new_tab('about:blank'),
							retries=1,
							check_driver_alive=False,
						),
					)
					await self._refresh_tabs()
					return self._handle_to_target.get(new_tab.handle, self.agent_focus_target_id or 'safari-target')
				except Exception:
					await safari_open_tab('about:blank', timeout_seconds=2.5)
					await asyncio.sleep(0.2)
					await self._refresh_tabs()
					if self._tabs_cache:
						return self._tabs_cache[-1].target_id
					return self.agent_focus_target_id or 'safari-target'
			target_id = self._tabs_cache[-1].target_id
		else:
			target_id = str(event.target_id)

		handle = self._target_to_handle.get(target_id)
		if handle is None:
			if await self._switch_tab_with_applescript_fallback(target_id):
				return target_id
			raise BrowserError(
				message=f'Tab {target_id[-4:]} not found',
				long_term_memory=f'Tab {target_id[-4:]} does not exist. Re-read tabs and retry.',
			)

		try:
			await self._with_retry(
				'SwitchTabEvent:switch_to_handle',
				lambda: self.driver.switch_to_handle(handle),
				retries=1,
				check_driver_alive=False,
			)
		except Exception as exc:
			self.logger.debug(f'WebDriver switch-tab failed for {target_id[-4:]}: {exc}')
			if await self._switch_tab_with_applescript_fallback(target_id):
				return target_id
			raise BrowserError(
				message=f'Tab {target_id[-4:]} switch failed',
				long_term_memory=f'Could not switch to tab {target_id[-4:]}. Re-read tabs and retry.',
			) from exc

		self.agent_focus_target_id = target_id
		await self._refresh_tabs()
		self._invalidate_dom_cache()
		self._append_recent_event(f'switch_tab:{target_id[-4:]}')
		return target_id

	async def on_CloseTabEvent(self, event: CloseTabEvent) -> None:
		await self.start()
		await self._refresh_tabs()
		target_id = str(event.target_id)
		handle = self._target_to_handle.get(target_id)
		if handle is None:
			await self._close_tab_with_applescript_fallback(target_id)
			return

		try:
			tabs = cast(
				list[Any],
				await self._with_retry('CloseTabEvent:list_tabs', self.driver.list_tabs, retries=1, check_driver_alive=False),
			)
		except Exception as exc:
			self.logger.debug(f'WebDriver list-tabs before close failed for {target_id[-4:]}: {exc}')
			await self._close_tab_with_applescript_fallback(target_id)
			return

		index = None
		for tab in tabs:
			if tab.handle == handle:
				index = tab.index
				break
		if index is None:
			await self._close_tab_with_applescript_fallback(target_id)
			return

		try:
			await self._with_retry(
				'CloseTabEvent:close_tab',
				lambda: self.driver.close_tab(index=index),
				retries=1,
				check_driver_alive=False,
			)
		except Exception as exc:
			self.logger.debug(f'WebDriver close-tab failed for {target_id[-4:]}: {exc}')
			await self._close_tab_with_applescript_fallback(target_id)
			return

		await self._refresh_tabs()
		self._invalidate_dom_cache()
		self._append_recent_event(f'close_tab:{target_id[-4:]}')

	async def on_GoBackEvent(self, event: GoBackEvent) -> None:
		del event
		await self.start()

		async def _go_back() -> None:
			await self.driver.go_back()
			await self.driver.wait_for_ready_state(timeout_seconds=8.0)
			await self._dismiss_dialogs_after_navigation(max_wait_seconds=0.8, poll_interval_seconds=0.15)
			self._invalidate_dom_cache()
			self._append_recent_event('go_back')

		await self._with_retry('GoBackEvent', _go_back)

	async def on_GoForwardEvent(self, event: GoForwardEvent) -> None:
		del event
		await self.start()

		async def _go_forward() -> None:
			await self.driver.go_forward()
			await self.driver.wait_for_ready_state(timeout_seconds=8.0)
			await self._dismiss_dialogs_after_navigation(max_wait_seconds=0.8, poll_interval_seconds=0.15)
			self._invalidate_dom_cache()
			self._append_recent_event('go_forward')

		await self._with_retry('GoForwardEvent', _go_forward)

	async def on_RefreshEvent(self, event: RefreshEvent) -> None:
		del event
		await self.start()

		async def _refresh() -> None:
			await self.driver.refresh()
			await self.driver.wait_for_ready_state(timeout_seconds=8.0)
			await self._dismiss_dialogs_after_navigation(max_wait_seconds=0.8, poll_interval_seconds=0.15)
			self._invalidate_dom_cache()
			self._append_recent_event('refresh')

		await self._with_retry('RefreshEvent', _refresh)

	async def on_WaitEvent(self, event: WaitEvent) -> None:
		seconds = min(max(event.seconds, 0.0), event.max_seconds)
		await asyncio.sleep(seconds)
		try:
			await self._refresh_download_tracking(wait_for_new=False)
		except Exception as exc:
			self.logger.warning(f'Safari wait download refresh failed: {type(exc).__name__}: {exc}')
		self._append_recent_event(f'wait:{seconds:.2f}s')

	async def on_SendKeysEvent(self, event: SendKeysEvent) -> None:
		await self.start()

		async def _send_keys() -> None:
			await self.driver.send_keys(event.keys)
			await self._dismiss_dialog_if_any()
			self._invalidate_dom_cache()
			self._append_recent_event(f'send_keys:{event.keys}')

		await self._with_retry('SendKeysEvent', _send_keys)

	async def on_UploadFileEvent(self, event: UploadFileEvent) -> None:
		await self.start()

		async def _upload() -> None:
			ref = await self._resolve_element_ref(event.node)

			if not ref.css_selector and not ref.xpath:
				raise BrowserError(
					message=f'Element {event.node.backend_node_id} has no selector for upload',
					long_term_memory='Could not locate file input element for upload.',
				)

			if ref.css_selector:
				await self.driver.upload_file(ref.css_selector, event.file_path, by='css')
			elif ref.xpath:
				await self.driver.upload_file(ref.xpath, event.file_path, by='xpath')

			self._invalidate_dom_cache()
			self._append_recent_event(f'upload:{Path(event.file_path).name}')

		await self._with_retry('UploadFileEvent', _upload)

	async def on_GetDropdownOptionsEvent(self, event: GetDropdownOptionsEvent) -> dict[str, str]:
		await self.start()

		async def _read_dropdown_options() -> dict[str, str]:
			ref = await self._resolve_element_ref(event.node)
			result = await self.driver.execute_js(
				"""
				function resolveElement(cssSelector, xpathSelector) {
					let element = null;
					if (cssSelector) {
						try { element = document.querySelector(cssSelector); } catch (_) {}
					}
					if (!element && xpathSelector) {
						try {
							element = document.evaluate(xpathSelector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
						} catch (_) {}
					}
					return element;
				}

				const element = resolveElement(arguments[0], arguments[1]);
				if (!element) return { error: 'Dropdown element not found' };

				let options = [];
				let dropdownType = 'unknown';

				if (element.tagName && element.tagName.toLowerCase() === 'select') {
					dropdownType = 'select';
					options = Array.from(element.options).map((option, index) => ({
						index,
						text: option.textContent ? option.textContent.trim() : '',
						value: option.value || '',
						selected: option.selected,
					}));
				} else {
					dropdownType = element.getAttribute('role') || 'custom';
					const candidates = element.querySelectorAll('[role="option"], [role="menuitem"], option, li');
					options = Array.from(candidates).map((option, index) => ({
						index,
						text: option.textContent ? option.textContent.trim() : '',
						value: option.getAttribute('value') || option.getAttribute('data-value') || '',
						selected: option.getAttribute('aria-selected') === 'true' || option.selected === true,
					})).filter((option) => option.text || option.value);
				}

				return { type: dropdownType, options };
				""",
				ref.css_selector,
				ref.xpath,
			)

			if not isinstance(result, dict) or result.get('error'):
				error_msg = (
					str(result.get('error', 'Failed to read dropdown options'))
					if isinstance(result, dict)
					else 'Invalid dropdown data'
				)
				return {
					'error': error_msg,
					'short_term_memory': error_msg,
					'long_term_memory': error_msg,
				}

			options = result.get('options', [])
			if not isinstance(options, list) or not options:
				message = f'No dropdown options found for index {event.node.backend_node_id}.'
				return {
					'type': str(result.get('type', 'unknown')),
					'options': '[]',
					'short_term_memory': message,
					'long_term_memory': message,
				}

			formatted = []
			for option in options:
				if not isinstance(option, dict):
					continue
				text = json.dumps(str(option.get('text', '')))
				value = json.dumps(str(option.get('value', '')))
				selected_suffix = ' (selected)' if option.get('selected') else ''
				formatted.append(f'{option.get("index", 0)}: text={text}, value={value}{selected_suffix}')

			message = (
				f'Found {len(formatted)} dropdown options for index {event.node.backend_node_id}:\n'
				+ '\n'.join(formatted)
				+ f'\n\nUse select_dropdown(index={event.node.backend_node_id}, text=...) with exact text/value.'
			)
			self._append_recent_event(f'dropdown_options:{event.node.backend_node_id}')
			return {
				'type': str(result.get('type', 'unknown')),
				'options': json.dumps(options),
				'formatted_options': '\n'.join(formatted),
				'short_term_memory': message,
				'long_term_memory': f'Read dropdown options for index {event.node.backend_node_id}.',
			}

		return cast(dict[str, str], await self._with_retry('GetDropdownOptionsEvent', _read_dropdown_options))

	async def on_SelectDropdownOptionEvent(self, event: SelectDropdownOptionEvent) -> dict[str, str]:
		await self.start()

		async def _select_dropdown_option() -> dict[str, str]:
			ref = await self._resolve_element_ref(event.node)
			result = await self.driver.execute_js(
				"""
				function resolveElement(cssSelector, xpathSelector) {
					let element = null;
					if (cssSelector) {
						try { element = document.querySelector(cssSelector); } catch (_) {}
					}
					if (!element && xpathSelector) {
						try {
							element = document.evaluate(xpathSelector, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
						} catch (_) {}
					}
					return element;
				}

				const targetValue = String(arguments[2] || '').toLowerCase();
				const element = resolveElement(arguments[0], arguments[1]);
				if (!element) return { success: false, error: 'Dropdown element not found' };

				if (element.tagName && element.tagName.toLowerCase() === 'select') {
					for (const option of Array.from(element.options)) {
						const optionText = (option.textContent || '').trim().toLowerCase();
						const optionValue = String(option.value || '').toLowerCase();
						if (optionText === targetValue || optionValue === targetValue) {
							element.value = option.value;
							option.selected = true;
							element.dispatchEvent(new Event('input', { bubbles: true }));
							element.dispatchEvent(new Event('change', { bubbles: true }));
							return { success: true, message: `Selected option: ${option.textContent || option.value}` };
						}
					}
					return { success: false, error: `Option '${arguments[2]}' not found` };
				}

				const candidates = element.querySelectorAll('[role="option"], [role="menuitem"], li, option');
				for (const candidate of Array.from(candidates)) {
					const text = (candidate.textContent || '').trim();
					const value = candidate.getAttribute('value') || candidate.getAttribute('data-value') || '';
					if (text.toLowerCase() === targetValue || String(value).toLowerCase() === targetValue) {
						candidate.click();
						return { success: true, message: `Selected option: ${text || value}` };
					}
				}

				return { success: false, error: `Option '${arguments[2]}' not found` };
				""",
				ref.css_selector,
				ref.xpath,
				event.text,
			)

			if not isinstance(result, dict):
				result = {'success': False, 'error': 'Invalid dropdown response'}

			self._invalidate_dom_cache()
			self._append_recent_event(f'dropdown_select:{event.node.backend_node_id}:{event.text[:40]}')

			if result.get('success'):
				return {
					'success': 'true',
					'message': str(result.get('message', f'Selected option {event.text}')),
				}

			error_text = str(result.get('error', f"Failed to select '{event.text}'"))
			return {
				'success': 'false',
				'error': error_text,
				'short_term_memory': error_text,
				'long_term_memory': error_text,
			}

		return cast(dict[str, str], await self._with_retry('SelectDropdownOptionEvent', _select_dropdown_option))
