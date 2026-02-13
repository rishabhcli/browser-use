"""Tests for Safari mode wiring in the fast CLI session layer."""

import pytest

from browser_use.skill_cli.sessions import create_browser_session
from safari_session import SafariBrowserSession


@pytest.mark.asyncio
async def test_create_browser_session_returns_safari_adapter() -> None:
	"""`create_browser_session(..., mode='safari')` should return Safari adapter."""
	session = await create_browser_session(mode='safari', headed=False, profile=None)
	assert isinstance(session, SafariBrowserSession)


@pytest.mark.asyncio
async def test_create_browser_session_safari_ignores_profile() -> None:
	"""Safari mode should still construct when profile is provided."""
	session = await create_browser_session(mode='safari', headed=True, profile='Profile 1')
	assert isinstance(session, SafariBrowserSession)
