"""Compatibility capability probe for the Safari real-profile backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from browser_use.browser.backends.safari_backend import (
	MINIMUM_MACOS_VERSION,
	MINIMUM_SAFARI_VERSION,
	probe_local_safari_backend,
)

# Legacy constant retained for compatibility with older imports. The current
# Safari backend no longer requires a companion host socket.
DEFAULT_SAFARI_HOST_SOCKET = Path.home() / '.browser-use' / 'safari' / 'host.sock'


@dataclass(slots=True)
class SafariCapabilityReport:
	"""Local preflight report for the built-in Safari backend.

	`host_socket_path` and `host_reachable` are preserved for compatibility with
	older callers that expected the deprecated companion-host probe surface.
	"""

	safari_installed: bool
	safari_version: str | None
	macos_version: str | None
	host_socket_path: Path = DEFAULT_SAFARI_HOST_SOCKET
	host_reachable: bool = False
	supported: bool = False
	gui_scripting_available: bool = False
	screen_capture_available: bool = False
	issues: list[str] = field(default_factory=list)

	def raise_for_unsupported(self) -> None:
		if self.supported:
			return
		raise RuntimeError(self.to_error_message())

	def to_error_message(self) -> str:
		issues = '\n'.join(f'  - {issue}' for issue in self.issues) or '  - Unknown Safari backend error'
		return (
			'Safari real-profile backend is unavailable.\n'
			f'Observed Safari: {self.safari_version or "not installed"}\n'
			f'Observed macOS: {self.macos_version or "unknown"}\n'
			f'{issues}\n\n'
			f'This backend requires Safari {MINIMUM_SAFARI_VERSION}+ and macOS {MINIMUM_MACOS_VERSION}+.'
		)


def probe_safari_environment(socket_path: Path | None = None) -> SafariCapabilityReport:
	"""Inspect local Safari support for the built-in JXA backend.

	The optional `socket_path` argument is ignored by the current implementation
	but retained for compatibility with older tests and imports.
	"""
	report = probe_local_safari_backend()
	details = report.details
	issues = [report.reason] if report.reason else []

	if report.available and details.get('gui_scripting_available') is not True:
		issues.append('Accessibility / GUI scripting permission is not granted yet.')
	if report.available and details.get('screen_capture_available') is not True:
		issues.append('Screen Recording screenshots are not available yet.')

	return SafariCapabilityReport(
		safari_installed=details.get('safari_version') not in {None, '0.0.0'},
		safari_version=details.get('safari_version'),
		macos_version=details.get('macos_version'),
		host_socket_path=socket_path or DEFAULT_SAFARI_HOST_SOCKET,
		host_reachable=False,
		supported=report.available,
		gui_scripting_available=bool(details.get('gui_scripting_available')),
		screen_capture_available=bool(details.get('screen_capture_available')),
		issues=issues,
	)
