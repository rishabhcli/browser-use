"""Unit tests for Safari session download tracking and AppleScript tab merge."""

from pathlib import Path

import pytest

import safari_session.session as safari_session_module
from browser_use.browser.events import WaitEvent
from safari_session.applescript import SafariDownloadEntry, SafariTabEntry
from safari_session.driver import SafariTabInfo
from safari_session.session import SafariBrowserSession


@pytest.mark.asyncio
async def test_safari_download_tracking_detects_new_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""New completed files should be added to downloaded_files."""
	session = SafariBrowserSession()
	try:
		monkeypatch.setattr(session, '_download_directories', lambda: [tmp_path])

		# Initialize baseline.
		await session._refresh_download_tracking(wait_for_new=False)

		download_path = tmp_path / 'report.pdf'
		download_path.write_text('hello')

		detected = await session._refresh_download_tracking(wait_for_new=False)
		assert str(download_path.resolve()) in detected
		assert str(download_path.resolve()) in session.downloaded_files
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_safari_download_tracking_ignores_partial_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	"""Temporary partial download files should not be tracked."""
	session = SafariBrowserSession()
	try:
		monkeypatch.setattr(session, '_download_directories', lambda: [tmp_path])

		await session._refresh_download_tracking(wait_for_new=False)

		partial_path = tmp_path / 'movie.zip.crdownload'
		partial_path.write_text('partial')

		detected = await session._refresh_download_tracking(wait_for_new=False)
		assert detected == []
		assert str(partial_path.resolve()) not in session.downloaded_files
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_refresh_tabs_prefers_applescript_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
	"""AppleScript title/URL should enrich webdriver tab metadata when available."""
	session = SafariBrowserSession()
	try:

		async def fake_list_tabs() -> list[SafariTabInfo]:
			return [SafariTabInfo(index=0, handle='handle-1', url='about:blank', title='')]

		async def fake_get_window_handle() -> str:
			return 'handle-1'

		async def fake_applescript_tabs(max_age_seconds: float = 1.0) -> list[SafariTabEntry]:
			del max_age_seconds
			return [SafariTabEntry(title='Example Domain', url='https://example.com')]

		monkeypatch.setattr(session.driver, 'list_tabs', fake_list_tabs)
		monkeypatch.setattr(session.driver, 'get_window_handle', fake_get_window_handle)
		monkeypatch.setattr(session, '_get_applescript_tabs', fake_applescript_tabs)

		tabs = await session._refresh_tabs()
		assert len(tabs) == 1
		assert tabs[0].title == 'Example Domain'
		assert tabs[0].url == 'https://example.com'
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_download_tracking_uses_applescript_recent_downloads_fallback(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""When directory polling misses files, AppleScript recent-download fallback should track them."""
	session = SafariBrowserSession()
	try:
		download_path = tmp_path / 'invoice.pdf'
		download_path.write_text('pdf-bytes')

		# Start from an empty baseline snapshot.
		await session._refresh_download_tracking(wait_for_new=False)

		async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
			return {}

		async def fake_show_downloads_ui(timeout_seconds: float = 1.5) -> bool:
			del timeout_seconds
			return True

		async def fake_recent_downloads(limit: int = 25, timeout_seconds: float = 2.5) -> list[SafariDownloadEntry]:
			del limit, timeout_seconds
			return [SafariDownloadEntry(file_name='invoice.pdf', path=str(download_path))]

		monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
		monkeypatch.setattr(safari_session_module, 'safari_show_downloads_ui', fake_show_downloads_ui)
		monkeypatch.setattr(safari_session_module, 'safari_list_recent_downloads', fake_recent_downloads)

		detected = await session._refresh_download_tracking(wait_for_new=True, timeout_seconds=0.2)
		assert str(download_path.resolve()) in detected
		assert str(download_path.resolve()) in session.downloaded_files
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_download_tracking_ignores_show_downloads_ui_exception(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Download tracking should still use recent-download fallback when downloads UI trigger fails."""
	session = SafariBrowserSession()
	try:
		download_path = tmp_path / 'receipt.pdf'
		download_path.write_text('bytes')

		await session._refresh_download_tracking(wait_for_new=False)

		async def fake_snapshot_download_files() -> dict[str, tuple[int, float]]:
			return {}

		async def fake_show_downloads_ui(timeout_seconds: float = 1.5) -> bool:
			del timeout_seconds
			raise RuntimeError('ui not available')

		async def fake_recent_downloads(limit: int = 25, timeout_seconds: float = 2.5) -> list[SafariDownloadEntry]:
			del limit, timeout_seconds
			return [SafariDownloadEntry(file_name='receipt.pdf', path=str(download_path))]

		monkeypatch.setattr(session, '_snapshot_download_files', fake_snapshot_download_files)
		monkeypatch.setattr(safari_session_module, 'safari_show_downloads_ui', fake_show_downloads_ui)
		monkeypatch.setattr(safari_session_module, 'safari_list_recent_downloads', fake_recent_downloads)

		detected = await session._refresh_download_tracking(wait_for_new=True, timeout_seconds=0.2)
		assert str(download_path.resolve()) in detected
		assert str(download_path.resolve()) in session.downloaded_files
	finally:
		await session.event_bus.stop(clear=True, timeout=5)


@pytest.mark.asyncio
async def test_wait_event_refreshes_download_tracking(monkeypatch: pytest.MonkeyPatch) -> None:
	"""WaitEvent should trigger download tracking refresh for delayed downloads."""
	session = SafariBrowserSession()
	refresh_calls: list[bool] = []
	slept_for: list[float] = []

	async def fake_refresh_download_tracking(wait_for_new: bool = False, timeout_seconds: float = 1.5) -> list[str]:
		del timeout_seconds
		refresh_calls.append(wait_for_new)
		return []

	async def fake_sleep(seconds: float) -> None:
		slept_for.append(seconds)
		return None

	monkeypatch.setattr(session, '_refresh_download_tracking', fake_refresh_download_tracking)
	monkeypatch.setattr(safari_session_module.asyncio, 'sleep', fake_sleep)

	try:
		await session.on_WaitEvent(WaitEvent(seconds=0.4, max_seconds=1.0))
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert slept_for == [0.4]
	assert refresh_calls == [False]
