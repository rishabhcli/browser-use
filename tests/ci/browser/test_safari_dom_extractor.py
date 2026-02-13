"""Unit tests for Safari DOM extraction payload bridging."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from safari_session.dom_extractor import (
	EXTRACTION_SCRIPT,
	SafariDOMExtractionResult,
	SafariExtractedElement,
	extract_interactive_elements,
)


class _FakeDriver:
	def __init__(self, payload: dict[str, Any]) -> None:
		self.payload = payload
		self.calls: list[tuple[str, tuple[Any, ...]]] = []

	async def execute_js(self, script: str, *args: Any) -> dict[str, Any]:
		self.calls.append((script, args))
		return self.payload


def _minimal_payload() -> dict[str, Any]:
	return {
		'url': 'https://example.com',
		'title': 'Example',
		'viewport_width': 1440,
		'viewport_height': 900,
		'elements': [
			{
				'index': 1,
				'backend_node_id': 1,
				'tag_name': 'button',
				'text_content': 'Continue',
				'attributes': {'id': 'continue-btn', 'role': 'button'},
				'bounding_rect': {'x': 120.0, 'y': 300.0, 'width': 88.0, 'height': 32.0},
				'is_visible': True,
				'is_scrollable': False,
				'xpath': '//*[@id="continue-btn"]',
				'css_selector': '#continue-btn',
				'stable_id': 'button|continue-btn|||Continue|120|300|88|32',
			}
		],
	}


@pytest.mark.asyncio
async def test_extract_interactive_elements_returns_typed_result() -> None:
	driver = _FakeDriver(_minimal_payload())
	result = await extract_interactive_elements(driver=driver, max_elements=25)  # type: ignore[arg-type]

	assert isinstance(result, SafariDOMExtractionResult)
	assert result.url == 'https://example.com'
	assert result.title == 'Example'
	assert result.viewport_width == 1440
	assert result.viewport_height == 900
	assert len(result.elements) == 1
	assert isinstance(result.elements[0], SafariExtractedElement)
	assert result.elements[0].tag_name == 'button'
	assert result.elements[0].attributes['id'] == 'continue-btn'
	assert driver.calls
	assert driver.calls[0][0] == EXTRACTION_SCRIPT
	assert driver.calls[0][1] == (25,)


@pytest.mark.asyncio
async def test_extract_interactive_elements_clamps_invalid_max_elements_to_one() -> None:
	driver = _FakeDriver(_minimal_payload())
	await extract_interactive_elements(driver=driver, max_elements=0)  # type: ignore[arg-type]
	assert driver.calls
	assert driver.calls[0][1] == (1,)


@pytest.mark.asyncio
async def test_extract_interactive_elements_raises_on_invalid_payload() -> None:
	payload = _minimal_payload()
	del payload['elements'][0]['stable_id']
	driver = _FakeDriver(payload)

	with pytest.raises(ValidationError, match='stable_id'):
		await extract_interactive_elements(driver=driver, max_elements=5)  # type: ignore[arg-type]
