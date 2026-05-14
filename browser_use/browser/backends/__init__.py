"""Protocol-neutral browser backend primitives."""

from browser_use.browser.backends.base import BrowserBackend, BrowserStartResult, NavigationResult
from browser_use.browser.backends.capabilities import BrowserCapabilities

__all__ = [
	'BrowserBackend',
	'BrowserCapabilities',
	'BrowserStartResult',
	'NavigationResult',
]
