"""Browser control commands."""

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

import anyio

from browser_use.skill_cli.sessions import SessionInfo

logger = logging.getLogger(__name__)

COMMANDS = {
	'open',
	'click',
	'type',
	'input',
	'scroll',
	'back',
	'screenshot',
	'state',
	'switch',
	'close-tab',
	'keys',
	'select',
	'eval',
	'extract',
	'cookies',
	'wait',
	'hover',
	'dblclick',
	'rightclick',
	'get',
}


async def _execute_js(session: SessionInfo, js: str) -> Any:
	"""Execute JavaScript in the browser via CDP."""
	bs = session.browser_session
	# Get or create a CDP session for the focused target
	cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
	if not cdp_session:
		raise RuntimeError('No active browser session')

	result = await cdp_session.cdp_client.send.Runtime.evaluate(
		params={'expression': js, 'returnByValue': True},
		session_id=cdp_session.session_id,
	)
	return result.get('result', {}).get('value')


async def _get_element_center(session: SessionInfo, node: Any) -> tuple[float, float] | None:
	"""Get the center coordinates of an element."""
	bs = session.browser_session

	# Safari adapter already carries absolute positions in the DOM node.
	absolute_position = getattr(node, 'absolute_position', None)
	if absolute_position is not None:
		try:
			x = float(getattr(absolute_position, 'x', 0.0))
			y = float(getattr(absolute_position, 'y', 0.0))
			width = float(getattr(absolute_position, 'width', 0.0))
			height = float(getattr(absolute_position, 'height', 0.0))
			if width > 0 and height > 0:
				return x + width / 2, y + height / 2
		except Exception:
			pass

	if not hasattr(bs, 'cdp_client_for_node') or not hasattr(bs, 'get_element_coordinates'):
		return None

	try:
		cdp_session = await bs.cdp_client_for_node(node)
		session_id = cdp_session.session_id
		backend_node_id = node.backend_node_id

		# Scroll element into view first
		try:
			await cdp_session.cdp_client.send.DOM.scrollIntoViewIfNeeded(
				params={'backendNodeId': backend_node_id}, session_id=session_id
			)
			await asyncio.sleep(0.05)
		except Exception:
			pass

		# Get element coordinates
		element_rect = await bs.get_element_coordinates(backend_node_id, cdp_session)
		if element_rect:
			center_x = element_rect.x + element_rect.width / 2
			center_y = element_rect.y + element_rect.height / 2
			return center_x, center_y
		return None
	except Exception as e:
		logger.error(f'Failed to get element center: {e}')
		return None


async def handle(action: str, session: SessionInfo, params: dict[str, Any]) -> Any:
	"""Handle browser control command."""
	bs = session.browser_session

	if action == 'open':
		url = params['url']
		# Ensure URL has scheme
		if not url.startswith(('http://', 'https://', 'file://')):
			url = 'https://' + url

		from browser_use.browser.events import NavigateToUrlEvent

		await bs.event_bus.dispatch(NavigateToUrlEvent(url=url))
		result: dict[str, Any] = {'url': url}
		# Add live preview URL for cloud browsers
		if bs.browser_profile.use_cloud and bs.cdp_url:
			from urllib.parse import quote

			result['live_url'] = f'https://live.browser-use.com/?wss={quote(bs.cdp_url, safe="")}'
		return result

	elif action == 'click':
		from browser_use.browser.events import ClickElementEvent

		index = params['index']
		# Look up node from selector map
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(ClickElementEvent(node=node))
		return {'clicked': index}

	elif action == 'type':
		text = params['text']
		try:
			# CDP-first path for Chromium sessions.
			cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
			if not cdp_session:
				return {'error': 'No active browser session'}
			await cdp_session.cdp_client.send.Input.insertText(
				params={'text': text},
				session_id=cdp_session.session_id,
			)
		except Exception:
			# Safari fallback: write into active element using injected JS.
			import json as json_module

			inserted = await _execute_js(
				session,
				f"""
				(function() {{
					const text = {json_module.dumps(text)};
					const el = document.activeElement;
					if (!el) return false;
					const tag = (el.tagName || '').toLowerCase();
					const isTextLike =
						tag === 'textarea' ||
						(tag === 'input' && !['checkbox', 'radio', 'submit', 'button', 'file'].includes((el.type || '').toLowerCase())) ||
						el.isContentEditable;
					if (!isTextLike) return false;
					if (el.isContentEditable) {{
						el.textContent = (el.textContent || '') + text;
					}} else {{
						const start = typeof el.selectionStart === 'number' ? el.selectionStart : (el.value || '').length;
						const end = typeof el.selectionEnd === 'number' ? el.selectionEnd : (el.value || '').length;
						const value = el.value || '';
						el.value = value.slice(0, start) + text + value.slice(end);
						const next = start + text.length;
						if (typeof el.setSelectionRange === 'function') el.setSelectionRange(next, next);
					}}
					el.dispatchEvent(new Event('input', {{ bubbles: true }}));
					el.dispatchEvent(new Event('change', {{ bubbles: true }}));
					return true;
				}})()
				""",
			)
			if not inserted:
				return {'error': 'No active text input is focused'}
		return {'typed': text}

	elif action == 'input':
		from browser_use.browser.events import ClickElementEvent, TypeTextEvent

		index = params['index']
		text = params['text']
		# Look up node from selector map
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(ClickElementEvent(node=node))
		await bs.event_bus.dispatch(TypeTextEvent(node=node, text=text))
		return {'input': text, 'element': index}

	elif action == 'scroll':
		from browser_use.browser.events import ScrollEvent

		direction = params.get('direction', 'down')
		amount = params.get('amount', 500)
		await bs.event_bus.dispatch(ScrollEvent(direction=direction, amount=amount))
		return {'scrolled': direction, 'amount': amount}

	elif action == 'back':
		from browser_use.browser.events import GoBackEvent

		await bs.event_bus.dispatch(GoBackEvent())
		return {'back': True}

	elif action == 'screenshot':
		data = await bs.take_screenshot(full_page=params.get('full', False))

		if params.get('path'):
			path = Path(params['path'])
			await anyio.Path(str(path)).write_bytes(data)
			return {'saved': str(path), 'size': len(data)}

		# Return base64 encoded
		return {'screenshot': base64.b64encode(data).decode(), 'size': len(data)}

	elif action == 'state':
		# Return the same LLM representation that browser-use agents see
		state_text = await bs.get_state_as_text()
		return {'_raw_text': state_text}

	elif action == 'switch':
		from browser_use.browser.events import SwitchTabEvent

		tab_index = params['tab']
		if bs.session_manager:
			page_targets = bs.session_manager.get_all_page_targets()
		elif hasattr(bs, 'get_tabs'):
			page_targets = await bs.get_tabs()
		else:
			page_targets = []
		if tab_index < 0 or tab_index >= len(page_targets):
			return {'error': f'Invalid tab index {tab_index}. Available: 0-{len(page_targets) - 1}'}
		target_id = page_targets[tab_index].target_id
		await bs.event_bus.dispatch(SwitchTabEvent(target_id=target_id))
		return {'switched': tab_index}

	elif action == 'close-tab':
		from browser_use.browser.events import CloseTabEvent

		tab_index = params.get('tab')
		if bs.session_manager:
			page_targets = bs.session_manager.get_all_page_targets()
		elif hasattr(bs, 'get_tabs'):
			page_targets = await bs.get_tabs()
		else:
			page_targets = []
		if tab_index is not None:
			if tab_index < 0 or tab_index >= len(page_targets):
				return {'error': f'Invalid tab index {tab_index}. Available: 0-{len(page_targets) - 1}'}
			target_id = page_targets[tab_index].target_id
		else:
			# Close current/focused tab
			target_id = bs.session_manager.get_focused_target().target_id if bs.session_manager else None
			if target_id is None and hasattr(bs, 'agent_focus_target_id'):
				target_id = bs.agent_focus_target_id
			if target_id is None and page_targets:
				target_id = page_targets[-1].target_id
			if not target_id:
				return {'error': 'No focused tab to close'}
		await bs.event_bus.dispatch(CloseTabEvent(target_id=target_id))
		return {'closed': tab_index}

	elif action == 'keys':
		from browser_use.browser.events import SendKeysEvent

		keys = params['keys']
		await bs.event_bus.dispatch(SendKeysEvent(keys=keys))
		return {'sent': keys}

	elif action == 'select':
		from browser_use.browser.events import SelectDropdownOptionEvent

		index = params['index']
		value = params['value']
		# Look up node from selector map
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}
		await bs.event_bus.dispatch(SelectDropdownOptionEvent(node=node, text=value))
		return {'selected': value, 'element': index}

	elif action == 'eval':
		js = params['js']
		# Execute JavaScript via CDP
		result = await _execute_js(session, js)
		return {'result': result}

	elif action == 'extract':
		query = params['query']
		# This requires LLM integration
		# For now, return a placeholder
		return {'query': query, 'error': 'extract requires agent mode - use: browser-use run "extract ..."'}

	elif action == 'hover':
		index = params['index']
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}

		coords = await _get_element_center(session, node)
		if not coords:
			return {'error': 'Could not get element coordinates for hover'}

		center_x, center_y = coords
		if hasattr(bs, 'cdp_client_for_node'):
			cdp_session = await bs.cdp_client_for_node(node)
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': center_x, 'y': center_y},
				session_id=cdp_session.session_id,
			)
		else:
			hovered = await _execute_js(
				session,
				f"""
				(function() {{
					const el = document.elementFromPoint({center_x}, {center_y});
					if (!el) return false;
					const ev = new MouseEvent('mousemove', {{
						bubbles: true,
						cancelable: true,
						clientX: {center_x},
						clientY: {center_y},
					}});
					el.dispatchEvent(ev);
					return true;
				}})()
				""",
			)
			if not hovered:
				return {'error': 'Could not dispatch hover event'}
		return {'hovered': index}

	elif action == 'dblclick':
		index = params['index']
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}

		coords = await _get_element_center(session, node)
		if not coords:
			return {'error': 'Could not get element coordinates for double-click'}

		center_x, center_y = coords
		if hasattr(bs, 'cdp_client_for_node'):
			cdp_session = await bs.cdp_client_for_node(node)
			session_id = cdp_session.session_id

			# Move mouse to element
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': center_x, 'y': center_y},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			# Double click (clickCount: 2)
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mousePressed',
					'x': center_x,
					'y': center_y,
					'button': 'left',
					'clickCount': 2,
				},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseReleased',
					'x': center_x,
					'y': center_y,
					'button': 'left',
					'clickCount': 2,
				},
				session_id=session_id,
			)
		else:
			from browser_use.browser.events import ClickCoordinateEvent

			await bs.event_bus.dispatch(ClickCoordinateEvent(coordinate_x=int(center_x), coordinate_y=int(center_y)))
			await asyncio.sleep(0.05)
			await bs.event_bus.dispatch(ClickCoordinateEvent(coordinate_x=int(center_x), coordinate_y=int(center_y)))
		return {'double_clicked': index}

	elif action == 'rightclick':
		index = params['index']
		node = await bs.get_element_by_index(index)
		if node is None:
			return {'error': f'Element index {index} not found - page may have changed'}

		coords = await _get_element_center(session, node)
		if not coords:
			return {'error': 'Could not get element coordinates for right-click'}

		center_x, center_y = coords
		if hasattr(bs, 'cdp_client_for_node'):
			cdp_session = await bs.cdp_client_for_node(node)
			session_id = cdp_session.session_id

			# Move mouse to element
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={'type': 'mouseMoved', 'x': center_x, 'y': center_y},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			# Right click (button: 'right')
			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mousePressed',
					'x': center_x,
					'y': center_y,
					'button': 'right',
					'clickCount': 1,
				},
				session_id=session_id,
			)
			await asyncio.sleep(0.05)

			await cdp_session.cdp_client.send.Input.dispatchMouseEvent(
				params={
					'type': 'mouseReleased',
					'x': center_x,
					'y': center_y,
					'button': 'right',
					'clickCount': 1,
				},
				session_id=session_id,
			)
		else:
			right_clicked = await _execute_js(
				session,
				f"""
				(function() {{
					const el = document.elementFromPoint({center_x}, {center_y});
					if (!el) return false;
					const ev = new MouseEvent('contextmenu', {{
						bubbles: true,
						cancelable: true,
						clientX: {center_x},
						clientY: {center_y},
						button: 2,
					}});
					el.dispatchEvent(ev);
					return true;
				}})()
				""",
			)
			if not right_clicked:
				return {'error': 'Could not dispatch right-click event'}
		return {'right_clicked': index}

	elif action == 'cookies':
		cookies_command = params.get('cookies_command')

		if cookies_command == 'get':
			if hasattr(bs, '_cdp_get_cookies'):
				# Get cookies via direct CDP
				cookies = await bs._cdp_get_cookies()
			elif hasattr(bs, 'cookies'):
				cookies = await bs.cookies()
			else:
				return {'error': 'Cookie retrieval not supported for this browser backend'}
			# Convert Cookie objects to dicts
			cookie_list: list[dict[str, Any]] = []
			for c in cookies:
				cookie_dict: dict[str, Any] = {
					'name': c.get('name', ''),
					'value': c.get('value', ''),
					'domain': c.get('domain', ''),
					'path': c.get('path', '/'),
					'secure': c.get('secure', False),
					'httpOnly': c.get('httpOnly', False),
				}
				if 'sameSite' in c:
					cookie_dict['sameSite'] = c.get('sameSite')
				if 'expires' in c:
					cookie_dict['expires'] = c.get('expires')
				cookie_list.append(cookie_dict)

			# Filter by URL if provided
			url = params.get('url')
			if url:
				from urllib.parse import urlparse

				parsed = urlparse(url)
				domain = parsed.netloc
				cookie_list = [
					c
					for c in cookie_list
					if domain.endswith(str(c.get('domain', '')).lstrip('.'))
					or str(c.get('domain', '')).lstrip('.').endswith(domain)
				]

			return {'cookies': cookie_list}

		elif cookies_command == 'set':
			cookie_dict: dict[str, Any] = {
				'name': params['name'],
				'value': params['value'],
				'path': params.get('path', '/'),
				'secure': params.get('secure', False),
				'httpOnly': params.get('http_only', False),
			}

			if params.get('domain'):
				cookie_dict['domain'] = params['domain']
			if params.get('same_site'):
				cookie_dict['sameSite'] = params['same_site']
			if params.get('expires'):
				cookie_dict['expires'] = params['expires']

			# If no domain specified, get current URL's domain
			if not params.get('domain'):
				hostname = await _execute_js(session, 'window.location.hostname')
				if hostname:
					cookie_dict['domain'] = hostname

			try:
				if hasattr(bs, '_cdp_set_cookies'):
					from cdp_use.cdp.network import Cookie

					cookie_obj = Cookie(**cookie_dict)
					await bs._cdp_set_cookies([cookie_obj])
				else:
					driver = getattr(bs, 'driver', None)
					if driver is not None and hasattr(driver, 'set_cookie'):
						await driver.set_cookie(cookie_dict)
					else:
						return {'set': params['name'], 'success': False, 'error': 'Cookie setting is not supported'}
				return {'set': params['name'], 'success': True}
			except Exception as e:
				logger.error(f'Failed to set cookie: {e}')
				return {'set': params['name'], 'success': False, 'error': str(e)}

		elif cookies_command == 'clear':
			url = params.get('url')
			if url and hasattr(bs, '_cdp_get_cookies'):
				# Clear cookies only for specific URL domain
				from urllib.parse import urlparse

				cookies = await bs._cdp_get_cookies()
				parsed = urlparse(url)
				domain = parsed.netloc

				cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
				if cdp_session:
					for cookie in cookies:
						cookie_domain = str(cookie.get('domain', '')).lstrip('.')
						if domain.endswith(cookie_domain) or cookie_domain.endswith(domain):
							await cdp_session.cdp_client.send.Network.deleteCookies(
								params={
									'name': cookie.get('name', ''),
									'domain': cookie.get('domain'),
									'path': cookie.get('path', '/'),
								},
								session_id=cdp_session.session_id,
							)
			elif url:
				return {'error': 'Domain-scoped cookie clear requires a CDP-capable browser backend'}
			else:
				# Clear all cookies
				if hasattr(bs, '_cdp_clear_cookies'):
					await bs._cdp_clear_cookies()
				elif hasattr(bs, 'clear_cookies'):
					await bs.clear_cookies()
				else:
					return {'error': 'Cookie clear is not supported for this browser backend'}

			return {'cleared': True, 'url': url}

		elif cookies_command == 'export':
			import json

			if hasattr(bs, '_cdp_get_cookies'):
				# Get cookies via direct CDP
				cookies = await bs._cdp_get_cookies()
			elif hasattr(bs, 'cookies'):
				cookies = await bs.cookies()
			else:
				return {'error': 'Cookie export is not supported for this browser backend'}
			# Convert to list of dicts
			cookie_list: list[dict[str, Any]] = []
			for c in cookies:
				cookie_dict: dict[str, Any] = {
					'name': c.get('name', ''),
					'value': c.get('value', ''),
					'domain': c.get('domain', ''),
					'path': c.get('path', '/'),
					'secure': c.get('secure', False),
					'httpOnly': c.get('httpOnly', False),
				}
				if 'sameSite' in c:
					cookie_dict['sameSite'] = c.get('sameSite')
				if 'expires' in c:
					cookie_dict['expires'] = c.get('expires')
				cookie_list.append(cookie_dict)

			# Filter by URL if provided
			url = params.get('url')
			if url:
				from urllib.parse import urlparse

				parsed = urlparse(url)
				domain = parsed.netloc
				cookie_list = [
					c
					for c in cookie_list
					if domain.endswith(str(c.get('domain', '')).lstrip('.'))
					or str(c.get('domain', '')).lstrip('.').endswith(domain)
				]

			file_path = Path(params['file'])
			await anyio.Path(str(file_path)).write_text(json.dumps(cookie_list, indent=2))
			return {'exported': len(cookie_list), 'file': str(file_path)}

		elif cookies_command == 'import':
			import json

			file_path = Path(params['file'])
			async_file_path = anyio.Path(str(file_path))
			if not await async_file_path.exists():
				return {'error': f'File not found: {file_path}'}

			cookies = json.loads(await async_file_path.read_text())

			if hasattr(bs, '_cdp_set_cookies'):
				# Get CDP session for bulk cookie setting
				cdp_session = await bs.get_or_create_cdp_session(target_id=None, focus=False)
				if not cdp_session:
					return {'error': 'No active browser session'}

				# Build cookie list for bulk set
				cookie_list = []
				for c in cookies:
					cookie_params = {
						'name': c['name'],
						'value': c['value'],
						'domain': c.get('domain'),
						'path': c.get('path', '/'),
						'secure': c.get('secure', False),
						'httpOnly': c.get('httpOnly', False),
					}
					if c.get('sameSite'):
						cookie_params['sameSite'] = c['sameSite']
					if c.get('expires'):
						cookie_params['expires'] = c['expires']
					cookie_list.append(cookie_params)

				# Set all cookies in one call
				try:
					await cdp_session.cdp_client.send.Network.setCookies(
						params={'cookies': cookie_list},  # type: ignore[arg-type]
						session_id=cdp_session.session_id,
					)
					return {'imported': len(cookie_list), 'file': str(file_path)}
				except Exception as e:
					return {'error': f'Failed to import cookies: {e}'}

			driver = getattr(bs, 'driver', None)
			if driver is not None and hasattr(driver, 'set_cookie'):
				imported = 0
				for cookie in cookies:
					try:
						await driver.set_cookie(cookie)
						imported += 1
					except Exception:
						continue
				return {'imported': imported, 'file': str(file_path)}

			return {'error': 'Cookie import is not supported for this browser backend'}

		return {'error': 'Invalid cookies command. Use: get, set, clear, export, import'}

	elif action == 'wait':
		import json as json_module

		wait_command = params.get('wait_command')

		if wait_command == 'selector':
			timeout_seconds = params.get('timeout', 30000) / 1000.0
			state = params.get('state', 'visible')
			selector = params['selector']
			poll_interval = 0.1
			elapsed = 0.0

			while elapsed < timeout_seconds:
				# Build JS check based on state
				if state == 'attached':
					js = f'document.querySelector({json_module.dumps(selector)}) !== null'
				elif state == 'detached':
					js = f'document.querySelector({json_module.dumps(selector)}) === null'
				elif state == 'visible':
					js = f"""
						(function() {{
							const el = document.querySelector({json_module.dumps(selector)});
							if (!el) return false;
							const style = window.getComputedStyle(el);
							const rect = el.getBoundingClientRect();
							return style.display !== 'none' &&
								   style.visibility !== 'hidden' &&
								   style.opacity !== '0' &&
								   rect.width > 0 &&
								   rect.height > 0;
						}})()
					"""
				elif state == 'hidden':
					js = f"""
						(function() {{
							const el = document.querySelector({json_module.dumps(selector)});
							if (!el) return true;
							const style = window.getComputedStyle(el);
							const rect = el.getBoundingClientRect();
							return style.display === 'none' ||
								   style.visibility === 'hidden' ||
								   style.opacity === '0' ||
								   rect.width === 0 ||
								   rect.height === 0;
						}})()
					"""
				else:
					js = f'document.querySelector({json_module.dumps(selector)}) !== null'

				result = await _execute_js(session, js)
				if result:
					return {'selector': selector, 'found': True}

				await asyncio.sleep(poll_interval)
				elapsed += poll_interval

			return {'selector': selector, 'found': False}

		elif wait_command == 'text':
			import json as json_module

			timeout_seconds = params.get('timeout', 30000) / 1000.0
			text = params['text']
			poll_interval = 0.1
			elapsed = 0.0

			while elapsed < timeout_seconds:
				js = f"""
					(function() {{
						const text = {json_module.dumps(text)};
						return document.body.innerText.includes(text);
					}})()
				"""
				result = await _execute_js(session, js)
				if result:
					return {'text': text, 'found': True}

				await asyncio.sleep(poll_interval)
				elapsed += poll_interval

			return {'text': text, 'found': False}

		return {'error': 'Invalid wait command. Use: selector, text'}

	elif action == 'get':
		import json as json_module

		get_command = params.get('get_command')

		if get_command == 'title':
			title = await _execute_js(session, 'document.title')
			return {'title': title or ''}

		elif get_command == 'html':
			selector = params.get('selector')
			if selector:
				js = f'(function(){{ const el = document.querySelector({json_module.dumps(selector)}); return el ? el.outerHTML : null; }})()'
			else:
				js = 'document.documentElement.outerHTML'
			html = await _execute_js(session, js)
			return {'html': html or ''}

		elif get_command == 'text':
			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}
			# Use the node's text from our model
			text = node.get_all_children_text(max_depth=10) if node else ''
			return {'index': index, 'text': text}

		elif get_command == 'value':
			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}

			try:
				cdp_session = await bs.cdp_client_for_node(node)
				resolve_result = await cdp_session.cdp_client.send.DOM.resolveNode(
					params={'backendNodeId': node.backend_node_id},
					session_id=cdp_session.session_id,
				)
				object_id = resolve_result['object'].get('objectId')  # type: ignore[union-attr]

				if object_id:
					value_result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
						params={
							'objectId': object_id,
							'functionDeclaration': 'function() { return this.value; }',
							'returnByValue': True,
						},
						session_id=cdp_session.session_id,
					)
					value = value_result.get('result', {}).get('value')
					return {'index': index, 'value': value or ''}
				else:
					return {'index': index, 'value': ''}
			except Exception as e:
				logger.error(f'Failed to get element value: {e}')
				return {'index': index, 'value': node.attributes.get('value', '')}

		elif get_command == 'attributes':
			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}
			# Use the attributes from the node model
			attrs = node.attributes or {}
			return {'index': index, 'attributes': dict(attrs)}

		elif get_command == 'bbox':
			index = params['index']
			node = await bs.get_element_by_index(index)
			if node is None:
				return {'error': f'Element index {index} not found - page may have changed'}

			try:
				cdp_session = await bs.cdp_client_for_node(node)
				box_result = await cdp_session.cdp_client.send.DOM.getBoxModel(
					params={'backendNodeId': node.backend_node_id},
					session_id=cdp_session.session_id,
				)

				model = box_result['model']  # type: ignore[index]
				content = model.get('content', [])  # type: ignore[union-attr]

				if len(content) >= 8:
					# content is [x1, y1, x2, y2, x3, y3, x4, y4] - corners of the quad
					x = min(content[0], content[2], content[4], content[6])
					y = min(content[1], content[3], content[5], content[7])
					width = max(content[0], content[2], content[4], content[6]) - x
					height = max(content[1], content[3], content[5], content[7]) - y
					return {'index': index, 'bbox': {'x': x, 'y': y, 'width': width, 'height': height}}
				else:
					return {'index': index, 'bbox': {}}
			except Exception as e:
				logger.error(f'Failed to get element bbox: {e}')
				rect = node.absolute_position
				if rect is not None:
					return {
						'index': index,
						'bbox': {'x': rect.x, 'y': rect.y, 'width': rect.width, 'height': rect.height},
					}
				return {'index': index, 'bbox': {}}

		return {'error': 'Invalid get command. Use: title, html, text, value, attributes, bbox'}

	raise ValueError(f'Unknown browser action: {action}')
