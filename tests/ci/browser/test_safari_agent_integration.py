"""Smoke tests for Agent integration with SafariBrowserSession."""

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from browser_use import Agent
from browser_use.agent.views import AgentOutput
from browser_use.browser.session import BrowserSession
from browser_use.browser.views import BrowserStateSummary, TabInfo
from browser_use.dom.views import SerializedDOMState
from browser_use.llm import BaseChatModel
from browser_use.llm.views import ChatInvokeCompletion
from browser_use.tools.service import Tools
from safari_session import SafariBrowserSession


def _create_done_mock_llm() -> BaseChatModel:
	"""Create a mock LLM that immediately returns a successful done action."""
	tools = Tools()
	ActionModel = tools.registry.create_action_model()
	AgentOutputWithActions = AgentOutput.type_with_custom_actions(ActionModel)

	llm = AsyncMock(spec=BaseChatModel)
	llm.model = 'mock-llm'
	llm.provider = 'mock'
	llm.name = 'mock-llm'
	llm.model_name = 'mock-llm'
	llm._verified_api_keys = True

	done_action = """
	{
		"thinking": null,
		"evaluation_previous_goal": "Task completed",
		"memory": "Task completed",
		"next_goal": "Task completed",
		"action": [
			{
				"done": {
					"text": "Task completed successfully",
					"success": true
				}
			}
		]
	}
	"""

	async def mock_ainvoke(*args, **kwargs):
		output_format = None
		if len(args) >= 2:
			output_format = args[1]
		elif 'output_format' in kwargs:
			output_format = kwargs['output_format']

		if output_format is None:
			return ChatInvokeCompletion(completion=done_action, usage=None)

		fields = getattr(output_format, 'model_fields', {})
		if 'is_correct' in fields:
			return ChatInvokeCompletion(
				completion=output_format.model_validate({'is_correct': True, 'reason': 'Mock simple judge'}),
				usage=None,
			)
		if 'verdict' in fields:
			return ChatInvokeCompletion(
				completion=output_format.model_validate({'verdict': True, 'reasoning': 'Mock trace judge'}),
				usage=None,
			)

		if output_format == AgentOutputWithActions:
			parsed = AgentOutputWithActions.model_validate_json(done_action)
		else:
			parsed = output_format.model_validate_json(done_action)
		return ChatInvokeCompletion(completion=parsed, usage=None)

	llm.ainvoke.side_effect = mock_ainvoke
	return llm


@pytest.mark.asyncio
async def test_agent_runs_with_safari_session_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
	"""Agent should run at least one step with Safari session without import/runtime crashes."""
	session = SafariBrowserSession()

	async def fake_start(self: SafariBrowserSession) -> None:
		del self
		session._started = True

	async def fake_stop(self: SafariBrowserSession) -> None:
		del self
		session._started = False

	async def fake_get_browser_state_summary(
		self: SafariBrowserSession,
		include_screenshot: bool = True,
		cached: bool = False,
		include_recent_events: bool = False,
	) -> BrowserStateSummary:
		del self
		del include_screenshot, cached, include_recent_events
		return BrowserStateSummary(
			dom_state=SerializedDOMState(_root=None, selector_map={}),
			url='about:blank',
			title='Blank',
			tabs=[TabInfo(url='about:blank', title='Blank', target_id='safari-target', parent_target_id=None)],
			screenshot=None,
		)

	async def fake_get_current_page_url(self: SafariBrowserSession) -> str:
		del self
		return 'about:blank'

	monkeypatch.setattr(SafariBrowserSession, 'start', fake_start)
	monkeypatch.setattr(SafariBrowserSession, 'stop', fake_stop)
	monkeypatch.setattr(SafariBrowserSession, 'get_browser_state_summary', fake_get_browser_state_summary)
	monkeypatch.setattr(SafariBrowserSession, 'get_current_page_url', fake_get_current_page_url)

	try:
		agent = Agent(
			task='Mark this task as done',
			llm=_create_done_mock_llm(),
			browser_session=cast(BrowserSession, cast(Any, session)),
			use_vision=False,
		)
		history = await agent.run(max_steps=1)
		assert history.is_done() is True
		assert history.is_successful() is True
	finally:
		await session.event_bus.stop(clear=True, timeout=5)
