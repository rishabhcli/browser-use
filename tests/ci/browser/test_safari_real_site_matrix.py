"""Opt-in Safari integration matrix on real websites.

This suite is intentionally disabled by default because it requires:
- macOS Safari with Develop -> Allow Remote Automation enabled
- live network access

Enable with:
RUN_SAFARI_MATRIX=1 uv run pytest tests/ci/browser/test_safari_real_site_matrix.py
"""

import os

import pytest

from safari_session import SafariBrowserSession

RUN_MATRIX = os.getenv('RUN_SAFARI_MATRIX') == '1'

REAL_SITE_MATRIX = [
	('https://www.wikipedia.org', 'wikipedia'),
	('https://github.com', 'github'),
	('https://news.ycombinator.com', 'hacker news'),
	('https://www.amazon.com', 'amazon'),
	('https://www.google.com', 'google'),
]

MIN_SELECTOR_COUNT = {
	'wikipedia': 4,
	'github': 5,
	'hacker news': 5,
	'amazon': 5,
	'google': 3,
}


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize(('url', 'expected_hint'), REAL_SITE_MATRIX)
async def test_safari_real_site_matrix(url: str, expected_hint: str) -> None:
	if not RUN_MATRIX:
		pytest.skip('Set RUN_SAFARI_MATRIX=1 to run Safari real-site integration tests.')

	session = SafariBrowserSession()
	try:
		try:
			await session.start()
		except RuntimeError as exc:
			error_text = str(exc).lower()
			if 'allow remote automation' in error_text or 'webdriver is disabled' in error_text:
				pytest.skip(
					'Safari Remote Automation is disabled. Enable Safari Develop -> Allow Remote Automation to run matrix.'
				)
			raise
		await session.navigate(url)
		state = await session.get_browser_state_summary(include_screenshot=False)

		# Basic health checks for each site in the matrix.
		assert state.url
		assert state.title is not None
		assert len(state.tabs) >= 1
		assert len(state.dom_state.selector_map) >= MIN_SELECTOR_COUNT[expected_hint]

		# Ensure Safari DOM extraction produces an LLM-usable text representation.
		llm_state = state.dom_state.llm_representation()
		assert len(llm_state) >= 120
		assert '[' in llm_state and ']' in llm_state

		combined_text = f'{state.title} {state.url}'.lower()
		assert expected_hint in combined_text
	finally:
		await session.stop()
