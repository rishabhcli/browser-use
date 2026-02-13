"""DOM extraction for Safari via injected JavaScript."""

from pydantic import BaseModel, ConfigDict, Field

from safari_session.driver import SafariDriver


class SafariDOMRect(BaseModel):
	"""Viewport-relative element bounds."""

	model_config = ConfigDict(extra='forbid')

	x: float
	y: float
	width: float
	height: float


class SafariExtractedElement(BaseModel):
	"""Interactive element extracted from the page."""

	model_config = ConfigDict(extra='forbid')

	index: int
	backend_node_id: int
	tag_name: str
	text_content: str | None = None
	attributes: dict[str, str] = Field(default_factory=dict)
	bounding_rect: SafariDOMRect
	is_visible: bool
	is_scrollable: bool
	xpath: str | None = None
	css_selector: str | None = None
	stable_id: str


class SafariDOMExtractionResult(BaseModel):
	"""Result payload from DOM extraction."""

	model_config = ConfigDict(extra='forbid')

	url: str
	title: str
	viewport_width: int
	viewport_height: int
	elements: list[SafariExtractedElement]


EXTRACTION_SCRIPT = r"""
return (() => {
	const MAX = Math.max(1, Math.min(Number(arguments[0] ?? 400), 2000));

	const selectors = [
		'a[href]',
		'button',
		'input',
		'select',
		'textarea',
		'[role="button"]',
		'[role="link"]',
		'[role="checkbox"]',
		'[role="menuitem"]',
		'[role="tab"]',
		'[contenteditable=""]',
		'[contenteditable="true"]',
		'[tabindex]:not([tabindex="-1"])'
	];

	const attrs = [
		'id', 'class', 'aria-label', 'href', 'type', 'placeholder', 'name', 'value', 'role', 'title'
	];

	const getXPath = (el) => {
		if (!el || el.nodeType !== Node.ELEMENT_NODE) return null;
		if (el.id) return `//*[@id="${el.id}"]`;
		const parts = [];
		let node = el;
		while (node && node.nodeType === Node.ELEMENT_NODE && node !== document.body) {
			let index = 1;
			let sibling = node.previousElementSibling;
			while (sibling) {
				if (sibling.tagName === node.tagName) index += 1;
				sibling = sibling.previousElementSibling;
			}
			parts.unshift(`${node.tagName.toLowerCase()}[${index}]`);
			node = node.parentElement;
		}
		return '/' + parts.join('/');
	};

	const getCssSelector = (el) => {
		if (!el || el.nodeType !== Node.ELEMENT_NODE) return null;
		if (el.id) return `#${CSS.escape(el.id)}`;
		const classPart = (el.className && typeof el.className === 'string')
			? '.' + el.className.trim().split(/\s+/).slice(0, 2).map((c) => CSS.escape(c)).join('.')
			: '';
		return `${el.tagName.toLowerCase()}${classPart}`;
	};

	const isVisible = (el, rect) => {
		if (!el || !rect) return false;
		if (rect.width <= 0 || rect.height <= 0) return false;
		if (rect.bottom < 0 || rect.right < 0) return false;
		if (rect.top > window.innerHeight || rect.left > window.innerWidth) return false;

		const style = window.getComputedStyle(el);
		if (!style) return true;
		if (style.display === 'none') return false;
		if (style.visibility === 'hidden') return false;
		if (style.opacity === '0') return false;
		if (el.hasAttribute('hidden')) return false;
		if (el.getAttribute('aria-hidden') === 'true') return false;
		return true;
	};

	const all = Array.from(document.querySelectorAll(selectors.join(',')));
	const unique = [];
	const seen = new Set();

	for (const el of all) {
		if (!el || seen.has(el)) continue;
		seen.add(el);
		unique.push(el);
		if (unique.length >= MAX) break;
	}

	const elements = unique.map((el, i) => {
		const rect = el.getBoundingClientRect();
		const attributes = {};
		for (const key of attrs) {
			const value = el.getAttribute(key);
			if (value != null && value !== '') attributes[key] = String(value);
		}

		const textContent = (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 300);
		const stableParts = [
			el.tagName.toLowerCase(),
			attributes.id || '',
			attributes.name || '',
			attributes.role || '',
			attributes.href || '',
			textContent.slice(0, 80),
			Math.round(rect.x),
			Math.round(rect.y),
			Math.round(rect.width),
			Math.round(rect.height)
		];

		return {
			index: i + 1,
			backend_node_id: i + 1,
			tag_name: el.tagName.toLowerCase(),
			text_content: textContent || null,
			attributes,
			bounding_rect: {
				x: rect.x,
				y: rect.y,
				width: rect.width,
				height: rect.height
			},
			is_visible: isVisible(el, rect),
			is_scrollable: (el.scrollHeight > el.clientHeight) || (el.scrollWidth > el.clientWidth),
			xpath: getXPath(el),
			css_selector: getCssSelector(el),
			stable_id: stableParts.join('|')
		};
	});

	return {
		url: window.location.href,
		title: document.title || '',
		viewport_width: window.innerWidth,
		viewport_height: window.innerHeight,
		elements,
	};
})();
"""


async def extract_interactive_elements(driver: SafariDriver, max_elements: int = 400) -> SafariDOMExtractionResult:
	"""Extract interactive elements from the current page."""
	if max_elements < 1:
		max_elements = 1
	payload = await driver.execute_js(EXTRACTION_SCRIPT, max_elements)
	return SafariDOMExtractionResult.model_validate(payload)
