"""Safari DOM capture using JavaScript executed through WebDriver."""

from __future__ import annotations

from typing import Any

from browser_use.dom.serializer.serializer import DOMTreeSerializer
from browser_use.dom.views import (
	DOMRect,
	EnhancedAXNode,
	EnhancedAXProperty,
	EnhancedDOMTreeNode,
	EnhancedSnapshotNode,
	NodeType,
	SerializedDOMState,
)

from .webdriver_client import SafariWebDriverClient

SAFARI_NODE_ID_ATTR = 'data-browser-use-safari-id'


DOM_WALKER_SCRIPT = r"""
const callback = arguments[arguments.length - 1];
(() => {
  const includeAttributes = new Set([
    'title', 'type', 'checked', 'id', 'name', 'role', 'value', 'placeholder',
    'alt', 'aria-label', 'aria-expanded', 'data-state', 'aria-checked',
    'aria-valuemin', 'aria-valuemax', 'aria-valuenow', 'aria-placeholder',
    'pattern', 'min', 'max', 'minlength', 'maxlength', 'step', 'accept',
    'multiple', 'inputmode', 'autocomplete', 'aria-autocomplete', 'list',
    'contenteditable', 'href', 'target', 'disabled', 'selected', 'required',
    'tabindex'
  ]);
  const blockedTags = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'META', 'LINK', 'TITLE']);
  const interactiveTags = new Set(['A', 'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'DETAILS', 'SUMMARY', 'OPTION']);
  const interactiveRoles = new Set([
    'button', 'link', 'menuitem', 'option', 'radio', 'checkbox', 'tab',
    'textbox', 'combobox', 'slider', 'spinbutton', 'search', 'searchbox',
    'row', 'cell', 'gridcell'
  ]);
  const runId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  let nextNodeId = 1;

  function rectForElement(el) {
    const rect = el.getBoundingClientRect();
    return {
      x: rect.x + window.scrollX,
      y: rect.y + window.scrollY,
      viewportX: rect.x,
      viewportY: rect.y,
      width: rect.width,
      height: rect.height
    };
  }

  function isVisible(el, style, rect) {
    if (!style || style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
      return false;
    }
    if (rect.width <= 0 || rect.height <= 0) {
      return false;
    }
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    return rect.viewportX < viewportWidth && rect.viewportY < viewportHeight &&
      rect.viewportX + rect.width > 0 && rect.viewportY + rect.height > 0;
  }

  function isInteractive(el, style) {
    const tag = el.tagName;
    if (interactiveTags.has(tag)) {
      return true;
    }
    const role = (el.getAttribute('role') || '').toLowerCase();
    if (interactiveRoles.has(role)) {
      return true;
    }
    if (el.onclick || el.getAttribute('onclick')) {
      return true;
    }
    if (el.hasAttribute('tabindex') && el.getAttribute('tabindex') !== '-1') {
      return true;
    }
    if (el.isContentEditable) {
      return true;
    }
    if (style && style.cursor === 'pointer') {
      return true;
    }
    return false;
  }

  function attributesForElement(el, style, addressable) {
    const attrs = {};
    for (const attr of Array.from(el.attributes || [])) {
      if (includeAttributes.has(attr.name) || attr.name.startsWith('data-') || attr.name.startsWith('aria-')) {
        attrs[attr.name] = attr.value;
      }
    }
    if (addressable) {
      const safariId = `${runId}-${nextNodeId}`;
      el.setAttribute('__browser_use_safari_internal_id', safariId);
      el.setAttribute('data-browser-use-safari-id', safariId);
      attrs['data-browser-use-safari-id'] = safariId;
    }
    if (style && style.cursor) {
      attrs['cursor'] = style.cursor;
    }
    return attrs;
  }

  function textNodePayload(node) {
    const text = (node.nodeValue || '').replace(/\s+/g, ' ').trim();
    if (!text) {
      return null;
    }
    const nodeId = nextNodeId++;
    return {
      nodeId,
      backendNodeId: nodeId,
      nodeType: 3,
      nodeName: '#text',
      nodeValue: text,
      attributes: {},
      isScrollable: false,
      isVisible: true,
      rect: null,
      cursor: null,
      role: null,
      axName: text,
      hasJsClickListener: false,
      children: []
    };
  }

  function elementPayload(el) {
    if (blockedTags.has(el.tagName)) {
      return null;
    }
    const style = window.getComputedStyle(el);
    const rect = rectForElement(el);
    const visible = isVisible(el, style, rect);
    const interactive = visible && isInteractive(el, style);
    const nodeId = nextNodeId++;
    const scrollable = el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth;
    const attrs = attributesForElement(el, style, interactive || scrollable);
    const children = [];

    for (const child of Array.from(el.childNodes || [])) {
      const payload = walk(child);
      if (payload) {
        children.push(payload);
      }
    }

    if (el.shadowRoot) {
      for (const child of Array.from(el.shadowRoot.childNodes || [])) {
        const payload = walk(child);
        if (payload) {
          children.push(payload);
        }
      }
    }

    if (el.tagName === 'IFRAME') {
      try {
        const frameDocument = el.contentDocument;
        if (frameDocument && frameDocument.documentElement) {
          const payload = walk(frameDocument.documentElement);
          if (payload) {
            children.push(payload);
          }
        }
      } catch (error) {
        // Cross-origin frames are intentionally skipped by the local Safari WebDriver walker.
      }
    }

    const label = el.getAttribute('aria-label') || el.getAttribute('title') ||
      el.getAttribute('placeholder') || el.getAttribute('value') || '';

    return {
      nodeId,
      backendNodeId: nodeId,
      nodeType: 1,
      nodeName: el.tagName,
      nodeValue: '',
      attributes: attrs,
      isScrollable: scrollable,
      isVisible: visible,
      rect,
      cursor: style.cursor || null,
      role: el.getAttribute('role') || null,
      axName: label,
      hasJsClickListener: Boolean(el.onclick || el.getAttribute('onclick')),
      children
    };
  }

  function walk(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      return textNodePayload(node);
    }
    if (node.nodeType === Node.ELEMENT_NODE) {
      return elementPayload(node);
    }
    return null;
  }

  const root = walk(document.documentElement);
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
  const pageWidth = Math.max(
    document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0, viewportWidth
  );
  const pageHeight = Math.max(
    document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0, viewportHeight
  );
  callback({
    root,
    pageInfo: {
      viewport_width: viewportWidth,
      viewport_height: viewportHeight,
      page_width: pageWidth,
      page_height: pageHeight,
      scroll_x: window.scrollX || 0,
      scroll_y: window.scrollY || 0,
      pixels_above: window.scrollY || 0,
      pixels_below: Math.max(0, pageHeight - viewportHeight - (window.scrollY || 0)),
      pixels_left: window.scrollX || 0,
      pixels_right: Math.max(0, pageWidth - viewportWidth - (window.scrollX || 0))
    }
  });
})();
"""


class SafariDomEngine:
	"""Build Browser-Use DOM state from Safari WebDriver JavaScript payloads."""

	def __init__(self, client: SafariWebDriverClient) -> None:
		self.client = client

	async def get_serialized_dom_tree(
		self, previous_state: SerializedDOMState | None = None
	) -> tuple[SerializedDOMState, dict[str, Any]]:
		"""Capture the current page DOM and return Browser-Use serialized state plus page info."""
		payload = await self.client.execute_async_script(DOM_WALKER_SCRIPT)
		if not isinstance(payload, dict) or not payload.get('root'):
			return SerializedDOMState(_root=None, selector_map={}), {}

		target_id = await self.client.current_window_handle()
		root = self._node_from_payload(payload['root'], target_id=target_id, parent=None)
		serializer = DOMTreeSerializer(
			root_node=root,
			previous_cached_state=previous_state,
			enable_bbox_filtering=True,
			paint_order_filtering=False,
			session_id=self.client.session_id,
		)
		state, _timing = serializer.serialize_accessible_elements()
		return state, payload.get('pageInfo', {})

	def _node_from_payload(
		self,
		payload: dict[str, Any],
		target_id: str,
		parent: EnhancedDOMTreeNode | None,
	) -> EnhancedDOMTreeNode:
		node_type = NodeType(payload.get('nodeType', 1))
		rect = payload.get('rect') or None
		absolute_position = self._rect_from_payload(rect, viewport=False) if rect else None
		snapshot_rect = self._rect_from_payload(rect, viewport=True) if rect else None
		attributes = {str(k): str(v) for k, v in (payload.get('attributes') or {}).items() if v is not None}
		ax_name = payload.get('axName') or attributes.get('aria-label') or attributes.get('title')

		node = EnhancedDOMTreeNode(
			node_id=int(payload.get('nodeId', 0)),
			backend_node_id=int(payload.get('backendNodeId', payload.get('nodeId', 0))),
			node_type=node_type,
			node_name=str(payload.get('nodeName', '')).upper(),
			node_value=str(payload.get('nodeValue', '')),
			attributes=attributes,
			is_scrollable=bool(payload.get('isScrollable', False)),
			is_visible=bool(payload.get('isVisible', True)),
			absolute_position=absolute_position,
			target_id=target_id,
			frame_id=None,
			session_id=self.client.session_id,
			content_document=None,
			shadow_root_type=None,
			shadow_roots=[],
			parent_node=parent,
			children_nodes=[],
			ax_node=EnhancedAXNode(
				ax_node_id=str(payload.get('nodeId', 0)),
				ignored=False,
				role=payload.get('role'),
				name=str(ax_name) if ax_name else None,
				description=None,
				properties=[EnhancedAXProperty(name='disabled', value=attributes.get('disabled') is not None)]
				if attributes.get('disabled') is not None
				else None,
				child_ids=None,
			),
			snapshot_node=EnhancedSnapshotNode(
				is_clickable=bool(payload.get('hasJsClickListener', False)),
				cursor_style=payload.get('cursor'),
				bounds=snapshot_rect,
				clientRects=snapshot_rect,
				scrollRects=None,
				computed_styles=None,
				paint_order=int(payload.get('nodeId', 0)),
				stacking_contexts=None,
			)
			if rect
			else None,
			has_js_click_listener=bool(payload.get('hasJsClickListener', False)),
		)

		children = [
			self._node_from_payload(child, target_id=target_id, parent=node)
			for child in payload.get('children', [])
			if isinstance(child, dict)
		]
		node.children_nodes = children
		return node

	def _rect_from_payload(self, rect: dict[str, Any], viewport: bool) -> DOMRect:
		if viewport:
			x_key = 'viewportX'
			y_key = 'viewportY'
		else:
			x_key = 'x'
			y_key = 'y'
		return DOMRect(
			x=float(rect.get(x_key, 0)),
			y=float(rect.get(y_key, 0)),
			width=float(rect.get('width', 0)),
			height=float(rect.get('height', 0)),
		)
