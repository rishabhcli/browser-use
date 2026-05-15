"""Safari backend event handlers."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from bubus import BaseEvent

from browser_use.browser.events import (
	BrowserStateRequestEvent,
	ClickCoordinateEvent,
	ClickElementEvent,
	GetDropdownOptionsEvent,
	GoBackEvent,
	GoForwardEvent,
	LoadStorageStateEvent,
	RefreshEvent,
	SaveStorageStateEvent,
	ScreenshotEvent,
	ScrollEvent,
	ScrollToTextEvent,
	SelectDropdownOptionEvent,
	SendKeysEvent,
	StorageStateLoadedEvent,
	StorageStateSavedEvent,
	TypeTextEvent,
	UploadFileEvent,
	WaitEvent,
)
from browser_use.browser.watchdog_base import BaseWatchdog


class SafariBackendWatchdog(BaseWatchdog):
	"""Routes browser state and default actions to the Safari backend."""

	LISTENS_TO: ClassVar[list[type[BaseEvent[Any]]]] = [
		BrowserStateRequestEvent,
		ClickElementEvent,
		ClickCoordinateEvent,
		TypeTextEvent,
		ScrollEvent,
		WaitEvent,
		GoBackEvent,
		GoForwardEvent,
		RefreshEvent,
		SendKeysEvent,
		UploadFileEvent,
		GetDropdownOptionsEvent,
		SelectDropdownOptionEvent,
		ScrollToTextEvent,
		ScreenshotEvent,
		SaveStorageStateEvent,
		LoadStorageStateEvent,
	]
	EMITS: ClassVar[list[type[BaseEvent[Any]]]] = [
		StorageStateSavedEvent,
		StorageStateLoadedEvent,
	]

	@property
	def safari_backend(self):
		return self.browser_session.require_browser_backend()

	async def on_BrowserStateRequestEvent(self, event: BrowserStateRequestEvent):
		state = await self.safari_backend.get_state(
			include_screenshot=event.include_screenshot,
			include_dom=event.include_dom,
		)
		if state.dom_state and state.dom_state.selector_map:
			self.browser_session.update_cached_selector_map(state.dom_state.selector_map)
		self.browser_session._cached_browser_state_summary = state
		if state.page_info:
			self.browser_session._original_viewport_size = (state.page_info.viewport_width, state.page_info.viewport_height)
		return state

	async def on_ClickElementEvent(self, event: ClickElementEvent):
		result = await self.safari_backend.click_element(event.node)
		self._clear_state_cache()
		return result

	async def on_ClickCoordinateEvent(self, event: ClickCoordinateEvent):
		result = await self.safari_backend.click_coordinates(event.coordinate_x, event.coordinate_y, force=event.force)
		self._clear_state_cache()
		return result

	async def on_TypeTextEvent(self, event: TypeTextEvent):
		result = await self.safari_backend.type_text(event.node, event.text, clear=event.clear)
		self._clear_state_cache()
		return result

	async def on_ScrollEvent(self, event: ScrollEvent):
		await self.safari_backend.scroll(event.amount, event.direction, node=event.node)
		self._clear_state_cache()

	async def on_WaitEvent(self, event: WaitEvent):
		await asyncio.sleep(min(max(event.seconds, 0), event.max_seconds))

	async def on_GoBackEvent(self, event: GoBackEvent):
		await self.safari_backend.go_back()
		self._clear_state_cache()

	async def on_GoForwardEvent(self, event: GoForwardEvent):
		await self.safari_backend.go_forward()
		self._clear_state_cache()

	async def on_RefreshEvent(self, event: RefreshEvent):
		await self.safari_backend.refresh()
		self._clear_state_cache()

	async def on_SendKeysEvent(self, event: SendKeysEvent):
		await self.safari_backend.send_keys(event.keys)
		self._clear_state_cache()

	async def on_UploadFileEvent(self, event: UploadFileEvent):
		await self.safari_backend.upload_file(event.node, event.file_path)
		self._clear_state_cache()

	async def on_GetDropdownOptionsEvent(self, event: GetDropdownOptionsEvent):
		return await self.safari_backend.get_dropdown_options(event.node)

	async def on_SelectDropdownOptionEvent(self, event: SelectDropdownOptionEvent):
		result = await self.safari_backend.select_dropdown_option(event.node, event.text)
		self._clear_state_cache()
		return result

	async def on_ScrollToTextEvent(self, event: ScrollToTextEvent):
		await self.safari_backend.scroll_to_text(event.text, direction=event.direction)
		self._clear_state_cache()

	async def on_ScreenshotEvent(self, event: ScreenshotEvent):
		return await self.safari_backend.screenshot(full_page=event.full_page, clip=event.clip)

	async def on_SaveStorageStateEvent(self, event: SaveStorageStateEvent):
		path = event.path or self.browser_session.browser_profile.storage_state
		if not path or isinstance(path, dict):
			return None
		state = await self.safari_backend.save_storage_state(str(path))
		self.event_bus.dispatch(
			StorageStateSavedEvent(
				path=str(path),
				cookies_count=len(state.get('cookies', [])),
				origins_count=len(state.get('origins', [])),
			)
		)
		return state

	async def on_LoadStorageStateEvent(self, event: LoadStorageStateEvent):
		path = event.path or self.browser_session.browser_profile.storage_state
		if not path:
			return None
		state = await self.safari_backend.load_storage_state(
			path if isinstance(path, dict) else str(path),
			defer_until_navigation=isinstance(path, dict),
		)
		self.event_bus.dispatch(
			StorageStateLoadedEvent(
				path=str(path),
				cookies_count=len(state.get('cookies', [])),
				origins_count=len(state.get('origins', [])),
			)
		)
		self._clear_state_cache()
		return state

	def _clear_state_cache(self) -> None:
		self.browser_session._cached_browser_state_summary = None
		self.browser_session._cached_selector_map = {}

	async def __aexit__(self, exc_type, exc_value, traceback):
		await asyncio.sleep(0)
