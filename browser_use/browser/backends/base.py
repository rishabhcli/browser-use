"""Browser backend protocol shared by CDP and WebDriver implementations."""

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from browser_use.browser.backends.capabilities import BrowserCapabilities
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import EnhancedDOMTreeNode


class BrowserStartResult(BaseModel):
	"""Result returned after a backend has established a usable browser session."""

	model_config = ConfigDict(extra='forbid')

	connection_url: str
	target_id: str
	capabilities: BrowserCapabilities


class NavigationResult(BaseModel):
	"""Result returned by backend navigation calls."""

	model_config = ConfigDict(extra='forbid')

	url: str
	status: int | None = None
	error_message: str | None = None


class BrowserBackend(Protocol):
	"""Protocol-neutral browser backend surface used below BrowserSession."""

	name: str
	capabilities: BrowserCapabilities

	async def start(self) -> BrowserStartResult: ...

	async def stop(self, force: bool = False) -> None: ...

	async def reconnect(self) -> None: ...

	async def get_tabs(self) -> list[TabInfo]: ...

	async def new_tab(self, url: str | None = None) -> str: ...

	async def switch_tab(self, tab_id: str) -> str: ...

	async def close_tab(self, tab_id: str) -> None: ...

	async def navigate(self, url: str, new_tab: bool = False) -> NavigationResult: ...

	async def go_back(self) -> None: ...

	async def go_forward(self) -> None: ...

	async def refresh(self) -> None: ...

	async def get_state(self, include_screenshot: bool, include_dom: bool) -> BrowserStateSummary: ...

	async def evaluate(self, code: str, await_promise: bool = True) -> Any: ...

	async def screenshot(self, full_page: bool = False, clip: dict | None = None) -> str: ...

	async def click_element(self, node: EnhancedDOMTreeNode) -> dict | None: ...

	async def click_coordinates(self, x: int, y: int, force: bool = False) -> dict | None: ...

	async def type_text(self, node: EnhancedDOMTreeNode, text: str, clear: bool) -> dict | None: ...

	async def send_keys(self, keys: str) -> None: ...

	async def scroll(self, amount: int, direction: str, node: EnhancedDOMTreeNode | None = None) -> None: ...

	async def upload_file(self, node: EnhancedDOMTreeNode, file_path: str) -> None: ...

	async def get_dropdown_options(self, node: EnhancedDOMTreeNode) -> dict[str, str]: ...

	async def select_dropdown_option(self, node: EnhancedDOMTreeNode, text: str) -> dict[str, str]: ...

	async def scroll_to_text(self, text: str, direction: str = 'down') -> None: ...

	async def save_storage_state(self, path: str) -> dict[str, Any]: ...

	async def load_storage_state(
		self, path_or_state: str | dict[str, Any], defer_until_navigation: bool = False
	) -> dict[str, Any]: ...

	async def downloaded_files(self) -> list[str]: ...

	async def print_to_pdf(self, options: dict[str, Any]) -> bytes: ...
