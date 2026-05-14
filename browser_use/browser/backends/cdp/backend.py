"""Adapter for the existing Chromium/CDP BrowserSession implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from browser_use.browser.backends.base import BrowserStartResult, NavigationResult
from browser_use.browser.backends.capabilities import BrowserCapabilities
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import EnhancedDOMTreeNode

if TYPE_CHECKING:
	from browser_use.browser.session import BrowserSession


CDP_CAPABILITIES = BrowserCapabilities(
	engine='chromium',
	protocol='cdp',
	supports_headless=True,
	supports_full_page_screenshot=True,
	supports_pdf_print=True,
	supports_download_events=True,
	supports_network_events=True,
	supports_permission_grants=True,
	supports_arbitrary_user_data_dir=True,
	supports_extension_loading=True,
	supports_proxy_per_session=True,
	supports_video_recording=True,
	supports_cross_origin_frame_dom=True,
	supports_bidi=False,
)


class CdpBrowserBackend:
	"""Thin compatibility backend for the current CDP BrowserSession path.

	This adapter intentionally delegates to the existing BrowserSession methods
	instead of moving CDP logic in the first slice. It gives tests and future
	backends a common capability surface without changing Chromium behavior.
	"""

	name = 'cdp'
	capabilities = CDP_CAPABILITIES

	def __init__(self, browser_session: BrowserSession) -> None:
		self.browser_session = browser_session

	async def start(self) -> BrowserStartResult:
		if not self.browser_session.cdp_url:
			raise RuntimeError('CDP backend start is owned by BrowserSession launch flow')
		return BrowserStartResult(
			connection_url=self.browser_session.cdp_url,
			target_id=self.browser_session.agent_focus_target_id or '',
			capabilities=self.capabilities,
		)

	async def stop(self, force: bool = False) -> None:
		if force:
			await self.browser_session.kill()
		else:
			await self.browser_session.stop()

	async def reconnect(self) -> None:
		await self.browser_session.reconnect()

	async def get_tabs(self) -> list[TabInfo]:
		return await self.browser_session.get_tabs()

	async def new_tab(self, url: str | None = None) -> str:
		return await self.browser_session._cdp_create_new_page(url or 'about:blank')

	async def switch_tab(self, tab_id: str) -> str:
		from browser_use.browser.events import SwitchTabEvent

		event = self.browser_session.event_bus.dispatch(SwitchTabEvent(target_id=tab_id))
		await event
		result = await event.event_result(raise_if_none=True, raise_if_any=True)
		return str(result)

	async def close_tab(self, tab_id: str) -> None:
		await self.browser_session._cdp_close_page(tab_id)

	async def navigate(self, url: str, new_tab: bool = False) -> NavigationResult:
		await self.browser_session.navigate_to(url, new_tab=new_tab)
		return NavigationResult(url=url)

	async def go_back(self) -> None:
		page = await self.browser_session.must_get_current_page()
		await page.go_back()

	async def go_forward(self) -> None:
		page = await self.browser_session.must_get_current_page()
		await page.go_forward()

	async def refresh(self) -> None:
		page = await self.browser_session.must_get_current_page()
		await page.reload()

	async def get_state(self, include_screenshot: bool, include_dom: bool) -> BrowserStateSummary:
		return await self.browser_session.get_browser_state_summary(include_screenshot=include_screenshot)

	async def evaluate(self, code: str, await_promise: bool = True) -> Any:
		cdp_session = await self.browser_session.get_or_create_cdp_session()
		result = await cdp_session.cdp_client.send.Runtime.evaluate(
			params={'expression': code, 'awaitPromise': await_promise, 'returnByValue': True},
			session_id=cdp_session.session_id,
		)
		return result.get('result', {}).get('value')

	async def screenshot(self, full_page: bool = False, clip: dict | None = None) -> str:
		import base64

		data = await self.browser_session.take_screenshot(full_page=full_page, clip=clip)
		return base64.b64encode(data).decode('utf-8')

	async def click_element(self, node: EnhancedDOMTreeNode) -> dict | None:
		raise NotImplementedError('CDP click_element is handled by DefaultActionWatchdog')

	async def click_coordinates(self, x: int, y: int, force: bool = False) -> dict | None:
		raise NotImplementedError('CDP click_coordinates is handled by DefaultActionWatchdog')

	async def type_text(self, node: EnhancedDOMTreeNode, text: str, clear: bool) -> dict | None:
		raise NotImplementedError('CDP type_text is handled by DefaultActionWatchdog')

	async def send_keys(self, keys: str) -> None:
		raise NotImplementedError('CDP send_keys is handled by DefaultActionWatchdog')

	async def scroll(self, amount: int, direction: str, node: EnhancedDOMTreeNode | None = None) -> None:
		raise NotImplementedError('CDP scroll is handled by DefaultActionWatchdog')

	async def upload_file(self, node: EnhancedDOMTreeNode, file_path: str) -> None:
		raise NotImplementedError('CDP upload_file is handled by DefaultActionWatchdog')

	async def get_dropdown_options(self, node: EnhancedDOMTreeNode) -> dict[str, str]:
		raise NotImplementedError('CDP dropdowns are handled by DefaultActionWatchdog')

	async def select_dropdown_option(self, node: EnhancedDOMTreeNode, text: str) -> dict[str, str]:
		raise NotImplementedError('CDP dropdowns are handled by DefaultActionWatchdog')

	async def scroll_to_text(self, text: str, direction: str = 'down') -> None:
		raise NotImplementedError('CDP scroll_to_text is handled by DefaultActionWatchdog')

	async def save_storage_state(self, path: str) -> dict[str, Any]:
		return await self.browser_session.export_storage_state(path)

	async def load_storage_state(self, path: str) -> dict[str, Any]:
		raise NotImplementedError('CDP storage loading is handled by StorageStateWatchdog')

	async def downloaded_files(self) -> list[str]:
		return self.browser_session.downloaded_files

	async def print_to_pdf(self, options: dict[str, Any]) -> bytes:
		raise NotImplementedError('CDP print_to_pdf is handled by DefaultActionWatchdog')
