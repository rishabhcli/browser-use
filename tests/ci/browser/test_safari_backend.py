import sys
from unittest.mock import AsyncMock
from urllib.parse import quote, urlparse

import pytest

from browser_use import Agent, Browser
from browser_use.browser.backends.safari import run_safari_doctor
from browser_use.browser.backends.safari.webdriver_client import SAFARI_TP_DRIVER
from browser_use.browser.events import ClickElementEvent, TypeTextEvent
from browser_use.browser.profile import BrowserChannel, BrowserEngine, BrowserProfile
from browser_use.llm import BaseChatModel
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.tools.service import Tools

pytestmark = pytest.mark.skipif(
	sys.platform != 'darwin' or not SAFARI_TP_DRIVER.exists(),
	reason='Safari Technology Preview safaridriver is required for Safari backend tests',
)


def test_safari_profile_and_backend_selection():
	profile = BrowserProfile(engine=BrowserEngine.SAFARI, channel=BrowserChannel.TECHNOLOGY_PREVIEW)
	browser = Browser(browser_profile=profile)

	assert profile.engine == BrowserEngine.SAFARI
	assert profile.channel == BrowserChannel.TECHNOLOGY_PREVIEW
	assert browser.require_browser_backend().name == 'safari'


async def test_safari_doctor_reports_working_stp_session():
	result = await run_safari_doctor(run_session_probe=True)

	assert result['checks']['technology_preview_driver']['status'] == 'ok'
	assert result['checks']['technology_preview_session']['status'] == 'ok'
	assert result['checks']['technology_preview_session']['js_result'] == 'OK'
	assert result['checks']['technology_preview_session']['screenshot_bytes'] > 0


async def test_safari_backend_minimal_state_click_and_type():
	browser = Browser(engine='safari', channel='technology-preview', window_size={'width': 900, 'height': 700})
	await browser.start()
	try:
		await browser.navigate_to(_smoke_url())
		state = await browser.get_browser_state_summary(include_screenshot=True)

		assert state.title == 'Safari Backend Smoke'
		assert state.screenshot
		assert state.tabs
		assert state.dom_state.selector_map

		input_node = next(node for node in state.dom_state.selector_map.values() if node.tag_name == 'input')
		type_event = browser.event_bus.dispatch(TypeTextEvent(node=input_node, text='Rishabh', clear=True))
		await type_event
		type_result = await type_event.event_result(raise_if_any=True, raise_if_none=False)
		assert isinstance(type_result, dict)
		assert type_result['actual_value'] == 'Rishabh'

		button_node = next(node for node in state.dom_state.selector_map.values() if node.tag_name == 'button')
		click_event = browser.event_bus.dispatch(ClickElementEvent(node=button_node))
		await click_event
		await click_event.event_result(raise_if_any=True, raise_if_none=False)

		assert await browser.require_browser_backend().evaluate('document.querySelector("#name").value') == 'Rishabh'
		assert await browser.require_browser_backend().evaluate('document.body.dataset.clicked') == 'yes'
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_agent_runs_one_stp_task_with_click_and_type():
	browser = Browser(engine='safari', channel='technology-preview', window_size={'width': 900, 'height': 700})
	await browser.start()
	try:
		await browser.navigate_to(_smoke_url())
		agent = Agent(
			task='Type Rishabh in the Name field, then click the button.',
			llm=_make_safari_smoke_llm(browser),
			browser=browser,
			use_judge=False,
			max_failures=2,
			directly_open_url=False,
		)

		history = await agent.run(max_steps=5)

		assert history.is_done()
		assert history.is_successful()
		assert history.action_names() == ['input', 'click', 'done']
		assert not [error for error in history.errors() if error]
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_default_tools_match_core_chrome_contract():
	browser = Browser(engine='safari', channel='technology-preview', window_size={'width': 900, 'height': 700})
	await browser.start()
	try:
		tools = Tools()
		await browser.navigate_to(_tools_url())
		state = await browser.get_browser_state_summary(include_screenshot=False)
		select_index = next(idx for idx, node in state.dom_state.selector_map.items() if node.tag_name == 'select')

		click_select = await tools.click(index=select_index, browser_session=browser)
		assert click_select.extracted_content
		assert 'Found select dropdown' in click_select.extracted_content
		assert 'Alpha' in click_select.extracted_content

		select_result = await tools.select_dropdown(index=select_index, text='Beta', browser_session=browser)
		assert not select_result.error
		assert await browser.require_browser_backend().evaluate('document.querySelector("#choice").value') == 'beta'
		assert await browser.require_browser_backend().evaluate('document.body.dataset.choice') == 'beta'

		search_result = await tools.search_page(pattern='NeedleText', browser_session=browser)
		assert search_result.extracted_content
		assert 'NeedleText' in search_result.extracted_content

		find_result = await tools.find_elements(
			selector='[data-role="item"]',
			attributes=['data-role'],
			browser_session=browser,
		)
		assert find_result.extracted_content
		assert 'data-role' in find_result.extracted_content

		evaluate_result = await tools.evaluate(
			code='document.querySelector("#choice").value + ":" + document.querySelectorAll("[data-role=item]").length',
			browser_session=browser,
		)
		assert evaluate_result.extracted_content == 'beta:2'

		await tools.navigate(url=_second_url(), new_tab=True, browser_session=browser)
		tabs = await browser.get_tabs()
		assert len(tabs) >= 2
		first_tab_id = next(tab.target_id[-4:] for tab in tabs if tab.title == 'Safari Tools Contract')
		switch_result = await tools.switch(tab_id=first_tab_id, browser_session=browser)
		assert f'#{first_tab_id}' in (switch_result.extracted_content or '')
		assert await browser.get_current_page_title() == 'Safari Tools Contract'

		second_tab_id = next(tab.target_id[-4:] for tab in await browser.get_tabs() if tab.title == 'Safari Second Tab')
		close_result = await tools.close(tab_id=second_tab_id, browser_session=browser)
		assert f'#{second_tab_id}' in (close_result.extracted_content or '')
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_storage_state_round_trip(httpserver, tmp_path):
	httpserver.expect_request('/storage').respond_with_data(
		"""
		<title>Safari Storage</title>
		<script>
			localStorage.setItem('browserUseLocal', 'persisted-local');
			sessionStorage.setItem('browserUseSession', 'persisted-session');
		</script>
		<body>Storage page</body>
		""",
		content_type='text/html',
	)
	httpserver.expect_request('/').respond_with_data('<title>Origin Root</title>', content_type='text/html')
	url = httpserver.url_for('/storage')
	storage_path = tmp_path / 'safari-storage.json'

	browser = Browser(engine='safari', channel='technology-preview', window_size={'width': 900, 'height': 700})
	await browser.start()
	try:
		await browser.navigate_to(url)
		state = await browser.export_storage_state(storage_path)
		assert storage_path.exists()
		assert state['origins']

		await browser.require_browser_backend().evaluate(
			"localStorage.removeItem('browserUseLocal'); sessionStorage.removeItem('browserUseSession');"
		)
		assert await browser.require_browser_backend().evaluate("localStorage.getItem('browserUseLocal')") is None

		await browser.require_browser_backend().load_storage_state(str(storage_path))
		assert await browser.require_browser_backend().evaluate("localStorage.getItem('browserUseLocal')") == 'persisted-local'
		assert (
			await browser.require_browser_backend().evaluate("sessionStorage.getItem('browserUseSession')") == 'persisted-session'
		)
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_loads_dict_storage_state_like_chrome(httpserver):
	httpserver.expect_request('/').respond_with_data('<title>Origin Root</title>', content_type='text/html')
	httpserver.expect_request('/check').respond_with_data('<title>Storage Check</title>', content_type='text/html')
	check_url = httpserver.url_for('/check')
	parsed = urlparse(check_url)
	origin = f'{parsed.scheme}://{parsed.netloc}'
	storage_state = {
		'cookies': [],
		'origins': [
			{
				'origin': origin,
				'localStorage': [{'name': 'fromDictLocal', 'value': 'local-ok'}],
				'sessionStorage': [{'name': 'fromDictSession', 'value': 'session-ok'}],
			}
		],
	}

	browser = Browser(
		engine='safari',
		channel='technology-preview',
		storage_state=storage_state,
		window_size={'width': 900, 'height': 700},
	)
	await browser.start()
	try:
		await browser.navigate_to(check_url)
		assert await browser.require_browser_backend().evaluate("localStorage.getItem('fromDictLocal')") == 'local-ok'
		assert await browser.require_browser_backend().evaluate("sessionStorage.getItem('fromDictSession')") == 'session-ok'
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_full_page_and_clip_screenshots_match_chrome_api():
	from io import BytesIO

	from PIL import Image

	browser = Browser(engine='safari', channel='technology-preview', window_size={'width': 700, 'height': 520})
	await browser.start()
	try:
		await browser.navigate_to(_tall_url())
		viewport = Image.open(BytesIO(await browser.take_screenshot(full_page=False)))
		full_page = Image.open(BytesIO(await browser.take_screenshot(full_page=True)))
		clip = Image.open(BytesIO(await browser.take_screenshot(clip={'x': 40, 'y': 760, 'width': 240, 'height': 160})))

		assert full_page.height > viewport.height
		assert full_page.width >= viewport.width
		assert clip.width > 0
		assert clip.height > 0
		assert 1.35 < clip.width / clip.height < 1.65
		assert await browser.require_browser_backend().evaluate('window.scrollY', await_promise=False) == 0
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_respects_navigation_security_policy():
	browser = Browser(
		engine='safari',
		channel='technology-preview',
		prohibited_domains=['blocked.example'],
		window_size={'width': 900, 'height': 700},
	)
	await browser.start()
	try:
		tools = Tools()
		await browser.navigate_to(_smoke_url())
		result = await tools.navigate(url='https://blocked.example/path', browser_session=browser)

		assert result.error
		assert 'blocked.example' in result.error
		assert await browser.get_current_page_url() == 'about:blank'
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


async def test_safari_interacts_with_shadow_dom_and_same_origin_iframe():
	browser = Browser(engine='safari', channel='technology-preview', window_size={'width': 900, 'height': 700})
	await browser.start()
	try:
		tools = Tools()
		await browser.navigate_to(_modern_dom_url())
		state = await browser.get_browser_state_summary(include_screenshot=False)

		def index_for(element_id: str) -> int:
			return next(idx for idx, node in state.dom_state.selector_map.items() if node.attributes.get('id') == element_id)

		await tools.input(index=index_for('shadowName'), text='Ada', browser_session=browser)
		shadow_click = await tools.click(index=index_for('shadowBtn'), browser_session=browser)
		assert not shadow_click.error
		assert await browser.require_browser_backend().evaluate('document.body.dataset.shadowClicked') == 'Ada'

		shadow_select = await tools.click(index=index_for('shadowChoice'), browser_session=browser)
		assert shadow_select.extracted_content
		assert 'Shadow Two' in shadow_select.extracted_content
		shadow_select_result = await tools.select_dropdown(
			index=index_for('shadowChoice'), text='Shadow Two', browser_session=browser
		)
		assert not shadow_select_result.error
		assert await browser.require_browser_backend().evaluate('document.body.dataset.shadowChoice') == 'two'

		await tools.scroll(index=index_for('shadowScroller'), pages=0.5, browser_session=browser)
		assert (
			await browser.require_browser_backend().evaluate(
				'document.querySelector("#host").shadowRoot.querySelector("#shadowScroller").scrollTop > 0'
			)
			is True
		)

		frame_click = await tools.click(index=index_for('frameBtn'), browser_session=browser)
		assert not frame_click.error
		assert (
			await browser.require_browser_backend().evaluate(
				'document.querySelector("#frame").contentDocument.body.dataset.frameClicked'
			)
			== 'yes'
		)
	finally:
		if browser.is_cdp_connected:
			await browser.kill()


def _smoke_url() -> str:
	html = (
		'<title>Safari Backend Smoke</title>'
		'<input id="name" placeholder="Name">'
		'<button id="go" onclick="document.body.dataset.clicked=\'yes\'; this.textContent=\'Clicked\';">'
		'Click me'
		'</button>'
	)
	return 'data:text/html,' + quote(html)


def _tools_url() -> str:
	html = (
		'<title>Safari Tools Contract</title>'
		'<label>Choice <select id="choice" onchange="document.body.dataset.choice=this.value">'
		'<option value="alpha">Alpha</option>'
		'<option value="beta">Beta</option>'
		'</select></label>'
		'<p>NeedleText appears here.</p>'
		'<div data-role="item">One</div>'
		'<div data-role="item">Two</div>'
	)
	return 'data:text/html,' + quote(html)


def _second_url() -> str:
	return 'data:text/html,' + quote('<title>Safari Second Tab</title><p>Second tab</p>')


def _modern_dom_url() -> str:
	from html import escape

	frame_html = '<button id="frameBtn" onclick="document.body.dataset.frameClicked=\'yes\'">Frame Button</button>'
	html = (
		'<title>Safari Modern DOM</title>'
		'<div id="host"></div>'
		f'<iframe id="frame" srcdoc="{escape(frame_html, quote=True)}"></iframe>'
		'<script>'
		'const root = document.querySelector("#host").attachShadow({mode: "open"});'
		'root.innerHTML = `<input id="shadowName" placeholder="Shadow name">'
		'<select id="shadowChoice"><option value="one">Shadow One</option><option value="two">Shadow Two</option></select>'
		'<div id="shadowScroller" style="height: 80px; overflow: auto;"><div style="height: 260px;">Scroll target</div></div>'
		'<button id="shadowBtn">Shadow Button</button>`;'
		'root.querySelector("#shadowBtn").addEventListener("click", () => {'
		' document.body.dataset.shadowClicked = root.querySelector("#shadowName").value;'
		'});'
		'root.querySelector("#shadowChoice").addEventListener("change", event => {'
		' document.body.dataset.shadowChoice = event.target.value;'
		'});'
		'</script>'
	)
	return 'data:text/html,' + quote(html)


def _tall_url() -> str:
	html = (
		'<title>Safari Screenshot</title>'
		'<style>html,body{margin:0;width:1200px;height:2200px;}'
		'.band{height:550px;font:24px sans-serif;padding:24px;}'
		'.a{background:#f7f1d4}.b{background:#d7f0ea}.c{background:#e8dcf5}.d{background:#f5ded8}</style>'
		'<section class="band a">Top</section>'
		'<section class="band b">Middle</section>'
		'<section class="band c">Clip area</section>'
		'<section class="band d">Bottom</section>'
	)
	return 'data:text/html,' + quote(html)


def _make_safari_smoke_llm(browser) -> BaseChatModel:
	llm = AsyncMock(spec=BaseChatModel)
	llm.model = 'mock-safari-agent'
	llm.provider = 'mock'
	llm.name = 'mock-safari-agent'
	llm.model_name = 'mock-safari-agent'
	llm._verified_api_keys = True
	step = 0

	async def ainvoke(_messages, output_format=None, **_kwargs):
		nonlocal step
		selector_map = (
			browser._cached_browser_state_summary.dom_state.selector_map
			if browser._cached_browser_state_summary
			else browser._cached_selector_map
		)
		input_idx = next((idx for idx, node in selector_map.items() if node.tag_name == 'input'), 1)
		button_idx = next((idx for idx, node in selector_map.items() if node.tag_name == 'button'), 1)

		if step == 0:
			raw = (
				'{"thinking":"Use input","evaluation_previous_goal":"Page ready","memory":"Need type",'
				f'"next_goal":"Type name","action":[{{"input":{{"index":{input_idx},"text":"Rishabh","clear":true}}}}]}}'
			)
		elif step == 1:
			raw = (
				'{"thinking":"Click button","evaluation_previous_goal":"Input filled","memory":"Need click",'
				f'"next_goal":"Click button","action":[{{"click":{{"index":{button_idx}}}}}]}}'
			)
		else:
			raw = (
				'{"thinking":"Done","evaluation_previous_goal":"Button clicked","memory":"Complete",'
				'"next_goal":"Done","action":[{"done":{"text":"Filled the Safari form and clicked the button.",'
				'"success":true}}]}'
			)
		step += 1
		assert output_format is not None
		return ChatInvokeCompletion(completion=output_format.model_validate_json(raw), usage=None)

	llm.ainvoke.side_effect = ainvoke
	return llm
