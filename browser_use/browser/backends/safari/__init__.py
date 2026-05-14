"""Safari WebDriver backend."""

from browser_use.browser.backends.safari.backend import SafariBrowserBackend
from browser_use.browser.backends.safari.diagnostics import run_safari_doctor

__all__ = ['SafariBrowserBackend', 'run_safari_doctor']
