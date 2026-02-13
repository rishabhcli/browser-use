"""Tests for Safari element re-finding after DOM changes."""

import pytest

import safari_session.session as safari_session_module
from browser_use.dom.views import DOMRect, EnhancedDOMTreeNode, NodeType, SerializedDOMState
from safari_session import SafariBrowserSession
from safari_session.dom_extractor import SafariDOMRect, SafariExtractedElement


def _make_document_node() -> EnhancedDOMTreeNode:
	"""Create a minimal document node for node-parent relationships in tests."""
	return EnhancedDOMTreeNode(
		node_id=0,
		backend_node_id=0,
		node_type=NodeType.DOCUMENT_NODE,
		node_name='#document',
		node_value='',
		attributes={},
		is_scrollable=False,
		is_visible=True,
		absolute_position=None,
		target_id='safari-target',
		frame_id='main',
		session_id='safari',
		content_document=None,
		shadow_root_type=None,
		shadow_roots=[],
		parent_node=None,
		children_nodes=[],
		ax_node=None,
		snapshot_node=None,
	)


@pytest.mark.asyncio
async def test_make_interactive_node_embeds_stable_id_attribute() -> None:
	"""Each interactive node should carry the stable-id attribute for re-finding."""
	session = SafariBrowserSession()
	element = SafariExtractedElement(
		index=1,
		backend_node_id=1,
		tag_name='button',
		text_content='Submit',
		attributes={},
		bounding_rect=SafariDOMRect(x=10, y=20, width=120, height=32),
		is_visible=True,
		is_scrollable=False,
		xpath='//button[1]',
		css_selector='button',
		stable_id='button||submit|10|20|120|32',
	)

	try:
		node = session._make_interactive_node(target_id='safari-target', element=element, parent=_make_document_node())
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert node.attributes.get('data-browser-use-stable-id') == element.stable_id


@pytest.mark.asyncio
async def test_resolve_element_ref_falls_back_to_stable_id(monkeypatch: pytest.MonkeyPatch) -> None:
	"""When backend IDs become stale, stable-id fallback should recover the element reference."""
	session = SafariBrowserSession()
	stable_id = 'button||submit|10|20|120|32'

	stale_node = EnhancedDOMTreeNode(
		node_id=999,
		backend_node_id=999,
		node_type=NodeType.ELEMENT_NODE,
		node_name='BUTTON',
		node_value='',
		attributes={'data-browser-use-stable-id': stable_id},
		is_scrollable=False,
		is_visible=True,
		absolute_position=DOMRect(x=10, y=20, width=120, height=32),
		target_id='safari-target',
		frame_id='main',
		session_id='safari',
		content_document=None,
		shadow_root_type=None,
		shadow_roots=[],
		parent_node=_make_document_node(),
		children_nodes=[],
		ax_node=None,
		snapshot_node=None,
	)

	async def fake_rebuild_interactive_dom_state() -> SerializedDOMState:
		session._ref_by_backend_id = {
			1: safari_session_module._SafariElementRef(
				backend_node_id=1,
				stable_id=stable_id,
				tag_name='button',
				text_content='Submit',
				attributes={},
				css_selector='button',
				xpath='//button[1]',
				absolute_position=DOMRect(x=10, y=20, width=120, height=32),
			)
		}
		return SerializedDOMState(_root=None, selector_map={})

	monkeypatch.setattr(session, '_rebuild_interactive_dom_state', fake_rebuild_interactive_dom_state)

	try:
		ref = await session._resolve_element_ref(stale_node)
	finally:
		await session.event_bus.stop(clear=True, timeout=5)

	assert ref.backend_node_id == 1
	assert ref.stable_id == stable_id
