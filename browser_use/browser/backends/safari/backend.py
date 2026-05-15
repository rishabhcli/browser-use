"""Safari WebDriver backend implementation."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from browser_use.browser.backends.base import BrowserStartResult, NavigationResult
from browser_use.browser.backends.capabilities import BrowserCapabilities
from browser_use.browser.backends.safari.dom_engine import SAFARI_NODE_ID_ATTR, SafariDomEngine
from browser_use.browser.backends.safari.webdriver_client import SafariDriverConfig, SafariWebDriverClient, WebDriverError
from browser_use.browser.views import BrowserStateSummary, PageInfo, TabInfo
from browser_use.dom.views import EnhancedDOMTreeNode, SerializedDOMState

if TYPE_CHECKING:
	from browser_use.browser.profile import BrowserProfile


SAFARI_CAPABILITIES = BrowserCapabilities(
	engine='safari',
	protocol='webdriver',
	supports_headless=False,
	supports_full_page_screenshot=True,
	supports_pdf_print=False,
	supports_download_events=False,
	supports_network_events=False,
	supports_permission_grants=False,
	supports_arbitrary_user_data_dir=False,
	supports_extension_loading=False,
	supports_proxy_per_session=False,
	supports_video_recording=False,
	supports_cross_origin_frame_dom=False,
	supports_bidi=False,
)


class SafariBrowserBackend:
	"""Safari Technology Preview/regular Safari backend over safaridriver."""

	name = 'safari'
	capabilities = SAFARI_CAPABILITIES

	def __init__(self, profile: BrowserProfile) -> None:
		channel = getattr(profile.channel, 'value', profile.channel)
		config = SafariDriverConfig.from_channel(
			str(channel) if channel else None,
			executable_path=profile.executable_path,
			env=profile.env,
			keep_alive=bool(profile.keep_alive),
		)
		self.profile = profile
		self.config = config
		self.client = SafariWebDriverClient(self.config)
		self.dom_engine = SafariDomEngine(self.client)
		self._last_state: BrowserStateSummary | None = None
		self._pending_storage_state: dict[str, Any] | None = None

	async def start(self) -> BrowserStartResult:
		"""Start safaridriver, create a session, and focus the first window."""
		last_error: BaseException | None = None
		for attempt in range(3):
			try:
				await self.client.start_driver()
				capabilities = await self.client.create_session()
				self.capabilities = self._capabilities_from_session(capabilities)
				await self._hide_browser_app_if_background()

				if self.profile.window_size:
					await self._set_window_rect(
						width=self.profile.window_size.width,
						height=self.profile.window_size.height,
						x=self.profile.window_position.width if self.profile.window_position else None,
						y=self.profile.window_position.height if self.profile.window_position else None,
					)
					await self._hide_browser_app_if_background()

				handles = await self.client.window_handles()
				if not handles:
					raise RuntimeError('Safari WebDriver session has no window handles')
				await self.client.switch_to_window(handles[0])
				return BrowserStartResult(
					connection_url=self.client.base_url,
					target_id=handles[0],
					capabilities=self.capabilities,
				)
			except Exception as exc:
				last_error = exc
				with contextlib.suppress(Exception):
					await self.client.close(force=True)
				if attempt < 2:
					await asyncio.sleep(1.0)
					self.client = SafariWebDriverClient(self.config)
					self.dom_engine = SafariDomEngine(self.client)

		assert last_error is not None
		raise last_error

	async def stop(self, force: bool = False) -> None:
		await self.client.close(force=force)
		self._last_state = None

	async def reconnect(self) -> None:
		if self.client.session_id:
			await self.client.status()
			return
		await self.start()

	async def get_tabs(self) -> list[TabInfo]:
		handles = await self.client.window_handles()
		current = await self.client.current_window_handle()
		tabs: list[TabInfo] = []
		for handle in handles:
			try:
				await self.client.switch_to_window(handle)
				tabs.append(
					TabInfo(
						target_id=handle,
						url=await self.client.current_url(),
						title=await self.client.title(),
						parent_target_id=None,
					)
				)
			except Exception:
				tabs.append(TabInfo(target_id=handle, url='', title='', parent_target_id=None))
		if current in handles:
			await self.client.switch_to_window(current)
		return tabs

	async def new_tab(self, url: str | None = None) -> str:
		handle = await self.client.new_window('tab')
		await self.client.switch_to_window(handle)
		await self._hide_browser_app_if_background()
		if url:
			await self.navigate(url)
		return handle

	async def switch_tab(self, tab_id: str) -> str:
		await self.client.switch_to_window(tab_id)
		await self._hide_browser_app_if_background()
		return tab_id

	async def close_tab(self, tab_id: str) -> None:
		current = await self.client.current_window_handle()
		await self.client.switch_to_window(tab_id)
		await self.client.close_window()
		remaining = await self.client.window_handles()
		if remaining:
			next_handle = current if current in remaining else remaining[-1]
			await self.client.switch_to_window(next_handle)

	async def navigate(self, url: str, new_tab: bool = False) -> NavigationResult:
		if new_tab:
			await self.new_tab(url)
		else:
			await self.client.navigate(url)
			await self._wait_for_ready_state()
		await self._apply_pending_storage_state_for_current_origin()
		await self._hide_browser_app_if_background()
		self._last_state = None
		return NavigationResult(url=await self.client.current_url())

	async def go_back(self) -> None:
		await self.client.go_back()
		await self._wait_for_ready_state()
		await self._hide_browser_app_if_background()
		self._last_state = None

	async def go_forward(self) -> None:
		await self.client.go_forward()
		await self._wait_for_ready_state()
		await self._hide_browser_app_if_background()
		self._last_state = None

	async def refresh(self) -> None:
		await self.client.refresh()
		await self._wait_for_ready_state()
		await self._hide_browser_app_if_background()
		self._last_state = None

	async def get_state(self, include_screenshot: bool, include_dom: bool) -> BrowserStateSummary:
		previous_dom = self._last_state.dom_state if self._last_state else None
		dom_state = SerializedDOMState(_root=None, selector_map={})
		page_info_data: dict[str, Any] = {}
		browser_errors: list[str] = []
		if include_dom:
			dom_state, page_info_data = await self.dom_engine.get_serialized_dom_tree(previous_dom)
		screenshot = None
		if include_screenshot:
			try:
				screenshot = await self.screenshot()
			except Exception as exc:
				browser_errors.append(f'Safari screenshot unavailable: {type(exc).__name__}: {exc}')
		tabs = await self.get_tabs()
		url = await self.client.current_url()
		title = await self.client.title()
		page_info = self._page_info_from_payload(page_info_data)

		state = BrowserStateSummary(
			dom_state=dom_state,
			url=url,
			title=title,
			tabs=tabs,
			screenshot=screenshot,
			page_info=page_info,
			pixels_above=page_info.pixels_above if page_info else 0,
			pixels_below=page_info.pixels_below if page_info else 0,
			browser_errors=browser_errors,
			is_pdf_viewer=url.lower().endswith('.pdf'),
			pending_network_requests=[],
			pagination_buttons=[],
			closed_popup_messages=[],
		)
		self._last_state = state
		return state

	async def evaluate(self, code: str, await_promise: bool = True) -> Any:
		"""Evaluate JavaScript in Safari.

		`code` may be either a JavaScript expression or a full function body with
		an explicit return statement.
		"""
		if await_promise:
			return await self.client.execute_async_script(
				"""
				const callback = arguments[arguments.length - 1];
				const source = arguments[0];
				let value;
				try {
					value = eval(source);
				} catch (evalError) {
					try {
						value = (new Function(source))();
					} catch (functionError) {
						callback({error: String(functionError), evalError: String(evalError)});
						return;
					}
				}
				Promise.resolve(value)
					.then((value) => callback(value))
					.catch((error) => callback({error: String(error)}));
				""",
				[code],
			)
		return await self.client.execute_script(
			"""
			const source = arguments[0];
			try {
				return eval(source);
			} catch (evalError) {
				return (new Function(source))();
			}
			""",
			[code],
		)

	async def screenshot(self, full_page: bool = False, clip: dict | None = None) -> str:
		if full_page or clip:
			return await self._stitched_screenshot(clip=clip if clip else None)
		last_error: BaseException | None = None
		for attempt in range(3):
			try:
				return await self.client.screenshot_base64()
			except Exception as exc:
				last_error = exc
				if attempt < 2:
					await asyncio.sleep(0.5)
		assert last_error is not None
		raise last_error

	async def screenshot_bytes(self, full_page: bool = False, clip: dict | None = None) -> bytes:
		return base64.b64decode(await self.screenshot(full_page=full_page, clip=clip))

	async def click_element(self, node: EnhancedDOMTreeNode) -> dict | None:
		index_for_logging = node.backend_node_id or 'unknown'
		if self._is_file_input(node):
			msg = (
				f'Index {index_for_logging} - has an element which opens file upload dialog. '
				'To upload files please use a specific function to upload files'
			)
			return {'validation_error': msg}
		if (node.tag_name or '').lower() == 'select':
			msg = 'Cannot click on <select> elements. Use dropdown_options action instead.'
			return {'validation_error': msg}

		selector = self._selector_for_node(node)
		try:
			element_id = await self._find_element(selector)
			await self._activate_element(element_id)
		except WebDriverError:
			click_result = await self._deep_click(selector)
			if not click_result.get('clicked'):
				raise WebDriverError(str(click_result.get('reason', f'Element not found for selector {selector}')))
		await asyncio.sleep(self.profile.wait_between_actions)
		self._last_state = None
		return {'selector': selector}

	async def click_coordinates(self, x: int, y: int, force: bool = False) -> dict | None:
		await self.client.execute_script(
			"""
			const x = arguments[0], y = arguments[1];
			const element = document.elementFromPoint(x, y);
			if (!element) return {clicked: false, reason: 'no element at point'};
			element.click();
			return {clicked: true, tagName: element.tagName};
			""",
			[x, y],
		)
		await asyncio.sleep(self.profile.wait_between_actions)
		self._last_state = None
		return {'click_x': x, 'click_y': y}

	async def type_text(self, node: EnhancedDOMTreeNode, text: str, clear: bool) -> dict | None:
		selector = self._selector_for_node(node)
		try:
			element_id = await self._find_element(selector)
			if clear:
				await self.client.clear_element(element_id)
			await self.client.type_element(element_id, text)
			actual_value = await self.client.element_value(element_id)
		except WebDriverError:
			type_result = await self._deep_type_text(selector, text, clear)
			if not type_result.get('typed'):
				raise WebDriverError(str(type_result.get('reason', f'Element not found for selector {selector}')))
			actual_value = type_result.get('actual_value')
		await asyncio.sleep(self.profile.wait_between_actions)
		self._last_state = None
		return {'selector': selector, 'actual_value': actual_value}

	async def send_keys(self, keys: str) -> None:
		await self.client.send_keys_to_active_element(keys)
		self._last_state = None

	async def scroll(self, amount: int, direction: str, node: EnhancedDOMTreeNode | None = None) -> None:
		sign = -1 if direction in {'up', 'left'} else 1
		x = amount * sign if direction in {'left', 'right'} else 0
		y = amount * sign if direction in {'up', 'down'} else 0
		if node:
			selector = self._selector_for_node(node)
			await self.client.execute_script(
				self._deep_query_script(
					"""
					element.scrollBy?.(arguments[1], arguments[2]);
					return true;
					""",
					include_action_args=False,
				),
				[selector, x, y],
			)
		else:
			await self.client.execute_script('window.scrollBy(arguments[0], arguments[1]);', [x, y])
		await asyncio.sleep(self.profile.wait_between_actions)
		self._last_state = None

	async def upload_file(self, node: EnhancedDOMTreeNode, file_path: str) -> None:
		if not self._is_file_input(node):
			index_for_logging = node.backend_node_id or 'unknown'
			raise RuntimeError(f'Upload failed - element {index_for_logging} is not a file input.')
		selector = self._selector_for_node(node)
		element_id = await self._find_element(selector)
		await self.client.type_element(element_id, str(Path(file_path).expanduser().resolve()))
		self._last_state = None

	async def get_dropdown_options(self, node: EnhancedDOMTreeNode) -> dict[str, str]:
		selector = self._selector_for_node(node)
		index_for_logging = node.backend_node_id or 'unknown'

		# Some ARIA/custom dropdowns only render their menu after focus/click.
		await self.client.execute_script(
			self._deep_query_script(
				"""
				element.focus?.();
				if (element.getAttribute('role') === 'combobox' || element.getAttribute('aria-haspopup')) {
					element.click?.();
				}
				return true;
				""",
				include_action_args=False,
			),
			[selector],
		)
		await asyncio.sleep(0.2)

		dropdown_data = await self.client.execute_script(
			"""
			const selector = arguments[0];
			function findDeep(root) {
				if (!root) return null;
				const direct = root.querySelector?.(selector);
				if (direct) return direct;
				const nodes = Array.from(root.querySelectorAll?.('*') || []);
				for (const node of nodes) {
					if (node.shadowRoot) {
						const shadowMatch = findDeep(node.shadowRoot);
						if (shadowMatch) return shadowMatch;
					}
					if (node.tagName === 'IFRAME') {
						try {
							const frameDocument = node.contentDocument;
							const frameMatch = findDeep(frameDocument);
							if (frameMatch) return frameMatch;
						} catch (error) {}
					}
				}
				return null;
			}

			const startElement = findDeep(document);
			if (!startElement) {
				return {error: `Element not found for selector ${selector}`};
			}

			function optionFromElement(item, index) {
				const text = (item.textContent || '').replace(/\\s+/g, ' ').trim();
				const value = item.getAttribute('value') || item.getAttribute('data-value') || text;
				return {
					text,
					value,
					index,
					selected: item.selected === true ||
						item.getAttribute('aria-selected') === 'true' ||
						item.classList.contains('selected') ||
						item.classList.contains('active')
				};
			}

			function nativeSelect(element) {
				if (element.tagName.toLowerCase() !== 'select') return null;
				return {
					type: 'select',
					options: Array.from(element.options).map((option, index) => ({
						text: option.text.trim(),
						value: option.value,
						index,
						selected: option.selected
					})),
					id: element.id || '',
					name: element.name || '',
					source: 'target'
				};
			}

			function ariaDropdown(element) {
				const role = element.getAttribute('role');
				const controls = element.getAttribute('aria-controls');
				const container = controls ? document.getElementById(controls) : element;
				if (!container) return null;
				if (!['menu', 'listbox', 'combobox'].includes(role || '') && !controls) return null;
				const items = Array.from(container.querySelectorAll('[role="menuitem"], [role="option"], option'));
				const options = items.map(optionFromElement).filter((option) => option.text || option.value);
				if (!options.length) return null;
				return {
					type: role === 'combobox' || controls ? 'aria-combobox' : 'aria',
					options,
					id: element.id || '',
					name: element.getAttribute('aria-label') || element.getAttribute('name') || '',
					source: controls ? 'aria-controls' : 'target'
				};
			}

			function customDropdown(element) {
				const candidates = Array.from(element.querySelectorAll('.item, .option, [data-value], li, button'));
				const options = candidates.map(optionFromElement).filter((option) => option.text || option.value);
				if (!options.length) return null;
				if (
					element.classList.contains('dropdown') ||
					element.classList.contains('ui') ||
					element.getAttribute('aria-haspopup') ||
					options.length > 1
				) {
					return {
						type: 'custom',
						options,
						id: element.id || '',
						name: element.getAttribute('aria-label') || '',
						source: 'target'
					};
				}
				return null;
			}

			function findDropdown(element, depth = 0) {
				if (!element || depth > 4) return null;
				const result = nativeSelect(element) || ariaDropdown(element) || customDropdown(element);
				if (result) return result;
				for (const child of Array.from(element.children || [])) {
					const childResult = findDropdown(child, depth + 1);
					if (childResult) {
						childResult.source = childResult.source === 'target' ? `child-depth-${depth + 1}` : childResult.source;
						return childResult;
					}
				}
				return null;
			}

			return findDropdown(startElement) || {
				error: `Element and its children are not recognizable dropdown types (tag: ${startElement.tagName}, role: ${startElement.getAttribute('role') || ''})`
			};
			""",
			[selector],
		)
		if not isinstance(dropdown_data, dict):
			raise RuntimeError(f'Unexpected dropdown response: {dropdown_data!r}')
		if dropdown_data.get('error'):
			msg = str(dropdown_data['error'])
			return {
				'error': msg,
				'short_term_memory': msg,
				'long_term_memory': msg,
				'backend_node_id': str(index_for_logging),
			}

		options = dropdown_data.get('options') or []
		if not options:
			msg = f'No options found in dropdown at index {index_for_logging}'
			return {
				'error': msg,
				'short_term_memory': msg,
				'long_term_memory': msg,
				'backend_node_id': str(index_for_logging),
			}

		formatted_options = []
		for option in options:
			text = str(option.get('text', ''))
			value = str(option.get('value', ''))
			status = ' (selected)' if option.get('selected') else ''
			formatted_options.append(
				f'{option.get("index", len(formatted_options))}: text={json.dumps(text)}, value={json.dumps(value)}{status}'
			)

		dropdown_type = str(dropdown_data.get('type', 'select'))
		element_info = (
			f'Index: {index_for_logging}, Type: {dropdown_type}, '
			f'ID: {dropdown_data.get("id", "none")}, Name: {dropdown_data.get("name", "none")}'
		)
		source_info = str(dropdown_data.get('source', 'unknown'))
		msg = f'Found {dropdown_type} dropdown ({element_info}):\n' + '\n'.join(formatted_options)
		msg += f'\n\nUse the exact text or value string (without quotes) in select_dropdown(index={index_for_logging}, text=...)'

		return {
			'type': dropdown_type,
			'options': json.dumps(options),
			'element_info': element_info,
			'source': source_info,
			'formatted_options': '\n'.join(formatted_options),
			'message': msg,
			'short_term_memory': msg,
			'long_term_memory': f'Got dropdown options for index {index_for_logging}',
			'backend_node_id': str(index_for_logging),
		}

	async def select_dropdown_option(self, node: EnhancedDOMTreeNode, text: str) -> dict[str, str]:
		selector = self._selector_for_node(node)
		index_for_logging = node.backend_node_id or 'unknown'
		result = await self.client.execute_script(
			"""
			const selector = arguments[0];
			const targetText = String(arguments[1]);
			function findDeep(root) {
				if (!root) return null;
				const direct = root.querySelector?.(selector);
				if (direct) return direct;
				const nodes = Array.from(root.querySelectorAll?.('*') || []);
				for (const node of nodes) {
					if (node.shadowRoot) {
						const shadowMatch = findDeep(node.shadowRoot);
						if (shadowMatch) return shadowMatch;
					}
					if (node.tagName === 'IFRAME') {
						try {
							const frameDocument = node.contentDocument;
							const frameMatch = findDeep(frameDocument);
							if (frameMatch) return frameMatch;
						} catch (error) {}
					}
				}
				return null;
			}

			const startElement = findDeep(document);
			if (!startElement) return {success: false, error: `Element not found for selector ${selector}`};
			const target = targetText.toLowerCase();

			function matchOptionText(text, value) {
				return String(text || '').trim().toLowerCase() === target ||
					String(value || '').trim().toLowerCase() === target;
			}

			function selectNative(element) {
				if (element.tagName.toLowerCase() !== 'select') return null;
				for (const option of Array.from(element.options)) {
					if (matchOptionText(option.text, option.value)) {
						element.focus();
						element.value = option.value;
						option.selected = true;
						element.selectedIndex = option.index;
						element.dispatchEvent(new Event('input', {bubbles: true, cancelable: true}));
						element.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
						element.blur();
						return {success: true, message: `Selected option: ${option.text.trim()} (value: ${option.value})`, value: option.value};
					}
				}
				return {
					success: false,
					error: `Option with text or value '${targetText}' not found in select element`,
					availableOptions: Array.from(element.options).map((option) => ({text: option.text.trim(), value: option.value}))
				};
			}

			function selectAriaOrCustom(element) {
				const controls = element.getAttribute('aria-controls');
				const containers = [element];
				if (controls) {
					const controlled = document.getElementById(controls);
					if (controlled) containers.push(controlled);
				}
				element.focus?.();
				if (controls || element.getAttribute('aria-haspopup')) element.click?.();
				const items = containers.flatMap((container) =>
					Array.from(container.querySelectorAll('[role="menuitem"], [role="option"], .item, .option, [data-value], li, button'))
				);
				for (const item of items) {
					const itemText = (item.textContent || '').replace(/\\s+/g, ' ').trim();
					const itemValue = item.getAttribute('value') || item.getAttribute('data-value') || itemText;
					if (matchOptionText(itemText, itemValue)) {
						item.setAttribute('aria-selected', 'true');
						item.classList.add('selected', 'active');
						item.click?.();
						item.dispatchEvent(new MouseEvent('click', {view: window, bubbles: true, cancelable: true}));
						element.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
						return {success: true, message: `Selected dropdown item: ${itemText}`, value: itemValue};
					}
				}
				return {
					success: false,
					error: `Dropdown item with text or value '${targetText}' not found`,
					availableOptions: items.map((item) => ({
						text: (item.textContent || '').replace(/\\s+/g, ' ').trim(),
						value: item.getAttribute('value') || item.getAttribute('data-value') || ''
					})).filter((option) => option.text || option.value)
				};
			}

			function selectFrom(element, depth = 0) {
				if (!element || depth > 4) return null;
				const nativeResult = selectNative(element);
				if (nativeResult) return nativeResult;
				const role = element.getAttribute('role');
				if (role || element.getAttribute('aria-controls') || element.getAttribute('aria-haspopup') || element.classList.contains('dropdown')) {
					const ariaResult = selectAriaOrCustom(element);
					if (ariaResult.success || ariaResult.availableOptions?.length) return ariaResult;
				}
				for (const child of Array.from(element.children || [])) {
					const childResult = selectFrom(child, depth + 1);
					if (childResult) return childResult;
				}
				return null;
			}

			return selectFrom(startElement) || {
				success: false,
				error: `Element and its children do not contain a dropdown with option '${targetText}'`
			};
			""",
			[selector, text],
		)
		if not isinstance(result, dict):
			raise RuntimeError(f'Unexpected dropdown selection response: {result!r}')
		if result.get('success'):
			msg = str(result.get('message', f'Selected option: {text}'))
			self._last_state = None
			return {
				'success': 'true',
				'message': msg,
				'value': str(result.get('value', text)),
				'backend_node_id': str(index_for_logging),
			}

		error_msg = str(result.get('error', f'Failed to select option: {text}'))
		available_options = result.get('availableOptions') or []
		if available_options:
			short_term_options = []
			for option in available_options:
				if isinstance(option, dict):
					option_text = str(option.get('text') or option.get('value') or '').strip()
				else:
					option_text = str(option).strip()
				if option_text:
					short_term_options.append(f'- {option_text}')
			if short_term_options:
				return {
					'success': 'false',
					'error': error_msg,
					'short_term_memory': 'Available dropdown options are:\n' + '\n'.join(short_term_options),
					'long_term_memory': f"Couldn't select the dropdown option as '{text}' is not one of the available options.",
					'backend_node_id': str(index_for_logging),
				}
		return {'success': 'false', 'error': error_msg, 'backend_node_id': str(index_for_logging)}

	async def scroll_to_text(self, text: str, direction: str = 'down') -> None:
		result = await self.client.execute_script(
			"""
			const needle = arguments[0].toLowerCase();
			const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				if ((node.nodeValue || '').toLowerCase().includes(needle)) {
					node.parentElement?.scrollIntoView({block: 'center'});
					return true;
				}
			}
			return false;
			""",
			[text],
		)
		if not result:
			raise RuntimeError(f'Text not found on page: {text}')
		self._last_state = None

	async def save_storage_state(self, path: str) -> dict[str, Any]:
		storage_state = await self._current_storage_state()
		if path:
			json_path = Path(path).expanduser().resolve()
			json_path.parent.mkdir(parents=True, exist_ok=True)
			json_path.write_text(json.dumps(storage_state, indent=2, ensure_ascii=False), encoding='utf-8')
		return storage_state

	async def load_storage_state(
		self, path_or_state: str | dict[str, Any], defer_until_navigation: bool = False
	) -> dict[str, Any]:
		if isinstance(path_or_state, dict):
			storage_state = path_or_state
		else:
			json_path = Path(path_or_state).expanduser().resolve()
			if not json_path.exists():
				return {'cookies': [], 'origins': []}
			storage_state = json.loads(json_path.read_text(encoding='utf-8'))
		if defer_until_navigation:
			self._pending_storage_state = storage_state
			return storage_state
		original_url = await self.client.current_url()
		try:
			for origin in storage_state.get('origins', []):
				origin_value = origin.get('origin')
				if not origin_value:
					continue
				await self.navigate(origin_value)
				await self.client.execute_script(
					"""
					const origin = arguments[0];
					const localItems = arguments[1] || [];
					const sessionItems = arguments[2] || [];
					if (window.location.origin !== origin) return false;
					for (const item of localItems) window.localStorage.setItem(item.name, item.value);
					for (const item of sessionItems) window.sessionStorage.setItem(item.name, item.value);
					return true;
					""",
					[origin_value, origin.get('localStorage') or [], origin.get('sessionStorage') or []],
				)

			for cookie in storage_state.get('cookies', []):
				if not isinstance(cookie, dict) or not cookie.get('name'):
					continue
				cookie_url = self._cookie_url(cookie)
				if cookie_url:
					await self.navigate(cookie_url)
				webdriver_cookie = self._webdriver_cookie(cookie)
				with contextlib.suppress(Exception):
					await self.client.add_cookie(webdriver_cookie)
		finally:
			if original_url:
				with contextlib.suppress(Exception):
					await self.navigate(original_url)
		return storage_state

	async def downloaded_files(self) -> list[str]:
		return []

	async def print_to_pdf(self, options: dict[str, Any]) -> bytes:
		try:
			pdf_base64 = await self.client.print_page(options)
		except WebDriverError as exc:
			raise NotImplementedError('Safari WebDriver on this machine does not expose print-to-PDF') from exc
		return base64.b64decode(pdf_base64)

	async def _stitched_screenshot(self, clip: dict | None = None) -> str:
		from PIL import Image

		metrics = await self._page_metrics()
		page_width = int(metrics.get('page_width') or metrics.get('viewport_width') or 1)
		page_height = int(metrics.get('page_height') or metrics.get('viewport_height') or 1)
		viewport_width = max(1, int(metrics.get('viewport_width') or page_width))
		viewport_height = max(1, int(metrics.get('viewport_height') or page_height))
		original_x = int(metrics.get('scroll_x') or 0)
		original_y = int(metrics.get('scroll_y') or 0)

		crop_left = max(0, int(float(clip.get('x', 0)))) if clip else 0
		crop_top = max(0, int(float(clip.get('y', 0)))) if clip else 0
		crop_width = max(1, int(float(clip.get('width', page_width)))) if clip else page_width
		crop_height = max(1, int(float(clip.get('height', page_height)))) if clip else page_height
		crop_right = min(page_width, crop_left + crop_width)
		crop_bottom = min(page_height, crop_top + crop_height)
		crop_width = max(1, crop_right - crop_left)
		crop_height = max(1, crop_bottom - crop_top)

		x_positions = self._tile_positions(crop_left, crop_right, viewport_width, max(0, page_width - viewport_width))
		y_positions = self._tile_positions(crop_top, crop_bottom, viewport_height, max(0, page_height - viewport_height))

		canvas: Image.Image | None = None
		scale_x = 1.0
		scale_y = 1.0
		try:
			for y in y_positions:
				for x in x_positions:
					await self.client.execute_script('window.scrollTo(arguments[0], arguments[1]);', [x, y])
					await asyncio.sleep(0.08)
					position = await self._page_metrics()
					actual_x = int(position.get('scroll_x') or x)
					actual_y = int(position.get('scroll_y') or y)

					image_data = base64.b64decode(await self.client.screenshot_base64())
					tile = Image.open(io.BytesIO(image_data)).convert('RGB')
					scale_x = tile.width / viewport_width
					scale_y = tile.height / viewport_height

					if canvas is None:
						canvas = Image.new(
							'RGB', (max(1, round(crop_width * scale_x)), max(1, round(crop_height * scale_y))), 'white'
						)

					visible_left = max(crop_left, actual_x)
					visible_top = max(crop_top, actual_y)
					visible_right = min(crop_right, actual_x + viewport_width)
					visible_bottom = min(crop_bottom, actual_y + viewport_height)
					if visible_left >= visible_right or visible_top >= visible_bottom:
						continue

					source_box = (
						round((visible_left - actual_x) * scale_x),
						round((visible_top - actual_y) * scale_y),
						round((visible_right - actual_x) * scale_x),
						round((visible_bottom - actual_y) * scale_y),
					)
					dest = (round((visible_left - crop_left) * scale_x), round((visible_top - crop_top) * scale_y))
					canvas.paste(tile.crop(source_box), dest)
		finally:
			with contextlib.suppress(Exception):
				await self.client.execute_script('window.scrollTo(arguments[0], arguments[1]);', [original_x, original_y])

		if canvas is None:
			return await self.client.screenshot_base64()

		output = io.BytesIO()
		canvas.save(output, format='PNG')
		return base64.b64encode(output.getvalue()).decode('ascii')

	async def _page_metrics(self) -> dict[str, Any]:
		metrics = await self.client.execute_script(
			"""
			const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 1;
			const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 1;
			const pageWidth = Math.max(
				document.documentElement.scrollWidth,
				document.body ? document.body.scrollWidth : 0,
				viewportWidth
			);
			const pageHeight = Math.max(
				document.documentElement.scrollHeight,
				document.body ? document.body.scrollHeight : 0,
				viewportHeight
			);
			return {
				viewport_width: viewportWidth,
				viewport_height: viewportHeight,
				page_width: pageWidth,
				page_height: pageHeight,
				scroll_x: window.scrollX || 0,
				scroll_y: window.scrollY || 0,
				device_pixel_ratio: window.devicePixelRatio || 1
			};
			"""
		)
		return metrics if isinstance(metrics, dict) else {}

	def _tile_positions(self, start: int, end: int, viewport_size: int, max_scroll: int) -> list[int]:
		if viewport_size <= 0:
			return [0]
		positions = []
		current = min(max_scroll, max(0, start))
		last_needed = max(0, end - viewport_size)
		while current < last_needed:
			positions.append(current)
			current = min(max_scroll, current + viewport_size)
			if positions and current == positions[-1]:
				break
		positions.append(min(max_scroll, max(0, last_needed)))
		return sorted(set(positions))

	async def _wait_for_ready_state(self, timeout: float = 10.0) -> None:
		deadline = asyncio.get_running_loop().time() + timeout
		while asyncio.get_running_loop().time() < deadline:
			try:
				ready_state = await self.client.execute_script('return document.readyState;')
				if ready_state in {'interactive', 'complete'}:
					return
			except Exception:
				pass
			await asyncio.sleep(0.1)

	async def _set_window_rect(self, width: int, height: int, x: int | None = None, y: int | None = None) -> None:
		payload: dict[str, int] = {'width': width, 'height': height}
		if x is not None:
			payload['x'] = x
		if y is not None:
			payload['y'] = y
		await self.client._session_request('POST', '/window/rect', json=payload)

	async def _hide_browser_app_if_background(self) -> None:
		import os

		if self.profile.headless is not True and os.getenv('BROWSER_USE_SAFARI_BACKGROUND') != '1':
			return
		process_name = self.client.config.browser_name
		script = f'tell application "System Events" to if exists process "{process_name}" then set visible of process "{process_name}" to false'
		with contextlib.suppress(Exception):
			process = await asyncio.create_subprocess_exec(
				'/usr/bin/osascript',
				'-e',
				script,
				stdout=asyncio.subprocess.DEVNULL,
				stderr=asyncio.subprocess.DEVNULL,
			)
			await asyncio.wait_for(process.wait(), timeout=2)

	def _selector_for_node(self, node: EnhancedDOMTreeNode) -> str:
		safari_id = node.attributes.get(SAFARI_NODE_ID_ATTR)
		if not safari_id:
			raise RuntimeError('Safari DOM node is missing its WebDriver selector id; refresh browser state and retry')
		return f'[{SAFARI_NODE_ID_ATTR}="{self._css_escape(safari_id)}"]'

	def _css_escape(self, value: str) -> str:
		return re.sub(r'(["\\])', r'\\\1', value)

	def _webdriver_element_key(self) -> str:
		from browser_use.browser.backends.safari.webdriver_client import WEBDRIVER_ELEMENT_KEY

		return WEBDRIVER_ELEMENT_KEY

	def _is_file_input(self, node: EnhancedDOMTreeNode) -> bool:
		return (node.tag_name or '').lower() == 'input' and node.attributes.get('type', '').lower() == 'file'

	async def _activate_element(self, element_id: str) -> None:
		await self.client.execute_script(
			"""
			const element = arguments[0];
			element.scrollIntoView?.({block: 'center', inline: 'center'});
			element.focus?.();
			element.click?.();
			""",
			[{self._webdriver_element_key(): element_id}],
		)

	async def _find_element(self, selector: str) -> str:
		try:
			return await self.client.find_element('css selector', selector)
		except WebDriverError:
			return await self.client.find_element_deep_css(selector)

	async def _deep_click(self, selector: str) -> dict[str, Any]:
		result = await self.client.execute_script(
			self._deep_query_script(
				"""
				element.scrollIntoView?.({block: 'center', inline: 'center'});
				element.click?.();
				element.dispatchEvent(new MouseEvent('click', {view: element.ownerDocument.defaultView, bubbles: true, cancelable: true}));
				return {clicked: true, tagName: element.tagName};
				"""
			),
			[selector],
		)
		return result if isinstance(result, dict) else {'clicked': False, 'reason': f'Unexpected click result: {result!r}'}

	async def _deep_type_text(self, selector: str, text: str, clear: bool) -> dict[str, Any]:
		result = await self.client.execute_script(
			self._deep_query_script(
				"""
				element.scrollIntoView?.({block: 'center', inline: 'center'});
				element.focus?.();
				if ('value' in element) {
					if (clear) element.value = '';
					element.value = `${element.value || ''}${text}`;
					element.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true, data: text, inputType: 'insertText'}));
					element.dispatchEvent(new Event('change', {bubbles: true, cancelable: true}));
					return {typed: true, actual_value: element.value};
				}
				if (element.isContentEditable) {
					if (clear) element.textContent = '';
					element.textContent = `${element.textContent || ''}${text}`;
					element.dispatchEvent(new InputEvent('input', {bubbles: true, cancelable: true, data: text, inputType: 'insertText'}));
					return {typed: true, actual_value: element.textContent || ''};
				}
				return {typed: false, reason: `Element ${element.tagName} is not text-editable`};
				"""
			),
			[selector, text, clear],
		)
		return result if isinstance(result, dict) else {'typed': False, 'reason': f'Unexpected type result: {result!r}'}

	def _deep_query_script(self, body: str, include_action_args: bool = True) -> str:
		action_args = (
			"""
		const text = arguments[1];
		const clear = Boolean(arguments[2]);
			"""
			if include_action_args
			else ''
		)
		return f"""
		const selector = arguments[0];
		{action_args}

		function findDeep(root) {{
			if (!root) return null;
			const direct = root.querySelector?.(selector);
			if (direct) return direct;
			const nodes = Array.from(root.querySelectorAll?.('*') || []);
			for (const node of nodes) {{
				if (node.shadowRoot) {{
					const shadowMatch = findDeep(node.shadowRoot);
					if (shadowMatch) return shadowMatch;
				}}
				if (node.tagName === 'IFRAME') {{
					try {{
						const frameDocument = node.contentDocument;
						const frameMatch = findDeep(frameDocument);
						if (frameMatch) return frameMatch;
					}} catch (error) {{}}
				}}
			}}
			return null;
		}}

		const element = findDeep(document);
		if (!element) return {{clicked: false, typed: false, reason: `Element not found for selector ${{selector}}`}};
		{body}
		"""

	async def _current_storage_state(self) -> dict[str, Any]:
		page_storage = await self.client.execute_script(
			"""
			const localStorageItems = [];
			const sessionStorageItems = [];
			try {
				for (let i = 0; i < window.localStorage.length; i++) {
					const name = window.localStorage.key(i);
					localStorageItems.push({name, value: window.localStorage.getItem(name)});
				}
			} catch (e) {}
			try {
				for (let i = 0; i < window.sessionStorage.length; i++) {
					const name = window.sessionStorage.key(i);
					sessionStorageItems.push({name, value: window.sessionStorage.getItem(name)});
				}
			} catch (e) {}
			return {
				origin: window.location.origin && window.location.origin !== 'null' ? window.location.origin : null,
				localStorage: localStorageItems,
				sessionStorage: sessionStorageItems
			};
			"""
		)
		origins = []
		if isinstance(page_storage, dict) and page_storage.get('origin'):
			origin_data: dict[str, Any] = {'origin': page_storage['origin']}
			if page_storage.get('localStorage'):
				origin_data['localStorage'] = page_storage['localStorage']
			if page_storage.get('sessionStorage'):
				origin_data['sessionStorage'] = page_storage['sessionStorage']
			if len(origin_data) > 1:
				origins.append(origin_data)

		cookies: list[dict[str, Any]] = []
		current_url = await self.client.current_url()
		if not current_url.startswith(('about:', 'data:', 'blob:')):
			with contextlib.suppress(Exception):
				cookies = [self._playwright_cookie(cookie) for cookie in await self.client.get_cookies()]
		return {'cookies': cookies, 'origins': origins}

	def _playwright_cookie(self, cookie: dict[str, Any]) -> dict[str, Any]:
		return {
			'name': cookie.get('name', ''),
			'value': cookie.get('value', ''),
			'domain': cookie.get('domain', ''),
			'path': cookie.get('path', '/'),
			'expires': cookie.get('expiry', cookie.get('expires', -1)),
			'httpOnly': cookie.get('httpOnly', False),
			'secure': cookie.get('secure', False),
			'sameSite': cookie.get('sameSite', 'Lax'),
		}

	def _webdriver_cookie(self, cookie: dict[str, Any]) -> dict[str, Any]:
		result: dict[str, Any] = {
			'name': cookie['name'],
			'value': cookie.get('value', ''),
			'path': cookie.get('path') or '/',
			'secure': bool(cookie.get('secure', False)),
			'httpOnly': bool(cookie.get('httpOnly', False)),
		}
		domain = str(cookie.get('domain') or '').lstrip('.')
		if domain:
			result['domain'] = domain
		expires = cookie.get('expires')
		if isinstance(expires, (int, float)) and expires > 0:
			result['expiry'] = int(expires)
		same_site = cookie.get('sameSite')
		if same_site in {'Strict', 'Lax', 'None'}:
			result['sameSite'] = same_site
		return result

	def _cookie_url(self, cookie: dict[str, Any]) -> str | None:
		if cookie.get('url'):
			return str(cookie['url'])
		domain = str(cookie.get('domain') or '').lstrip('.')
		if not domain:
			return None
		scheme = 'https' if cookie.get('secure') else 'http'
		path = str(cookie.get('path') or '/')
		return f'{scheme}://{domain}{path if path.startswith("/") else "/" + path}'

	async def _apply_pending_storage_state_for_current_origin(self) -> None:
		if not self._pending_storage_state:
			return
		current_origin = await self._current_origin()
		if not current_origin:
			return

		applied = False
		for origin in self._pending_storage_state.get('origins', []):
			if origin.get('origin') != current_origin:
				continue
			await self.client.execute_script(
				"""
				const localItems = arguments[0] || [];
				const sessionItems = arguments[1] || [];
				for (const item of localItems) window.localStorage.setItem(item.name, item.value);
				for (const item of sessionItems) window.sessionStorage.setItem(item.name, item.value);
				""",
				[origin.get('localStorage') or [], origin.get('sessionStorage') or []],
			)
			applied = True

		for cookie in self._pending_storage_state.get('cookies', []):
			if self._cookie_matches_origin(cookie, current_origin):
				with contextlib.suppress(Exception):
					await self.client.add_cookie(self._webdriver_cookie(cookie))
					applied = True

		if applied:
			self._pending_storage_state = None

	async def _current_origin(self) -> str | None:
		origin = await self.client.execute_script(
			'return window.location.origin && window.location.origin !== "null" ? window.location.origin : null;'
		)
		return str(origin) if origin else None

	def _cookie_matches_origin(self, cookie: dict[str, Any], origin: str) -> bool:
		origin_host = urlparse(origin).hostname or ''
		cookie_domain = str(cookie.get('domain') or '').lstrip('.')
		return bool(cookie_domain and (origin_host == cookie_domain or origin_host.endswith(f'.{cookie_domain}')))

	def _page_info_from_payload(self, payload: dict[str, Any]) -> PageInfo | None:
		if not payload:
			return None
		return PageInfo(
			viewport_width=int(payload.get('viewport_width', 0)),
			viewport_height=int(payload.get('viewport_height', 0)),
			page_width=int(payload.get('page_width', 0)),
			page_height=int(payload.get('page_height', 0)),
			scroll_x=int(payload.get('scroll_x', 0)),
			scroll_y=int(payload.get('scroll_y', 0)),
			pixels_above=int(payload.get('pixels_above', 0)),
			pixels_below=int(payload.get('pixels_below', 0)),
			pixels_left=int(payload.get('pixels_left', 0)),
			pixels_right=int(payload.get('pixels_right', 0)),
		)

	def _capabilities_from_session(self, capabilities: dict[str, Any]) -> BrowserCapabilities:
		return self.capabilities.model_copy(
			update={
				'supports_bidi': bool(capabilities.get('webSocketUrl')),
			}
		)
