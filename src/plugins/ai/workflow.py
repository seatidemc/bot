"""AI 消息工作流：router 选择 agent → agent loop → 最终回复。"""

from __future__ import annotations

from dataclasses import dataclass

from . import agents


@dataclass(frozen=True)
class WorkflowResult:
    reply: str
    intent: str
    reason: str


async def run(text: str, history: list[dict[str, str]]) -> WorkflowResult:
    """路由并执行一次完整的 AI 对话。"""
    decision = await agents.route(text, history)
    agent = agents.get_agent(decision.intent)
    reply = await agents.run_agent(agent, text, history)
    return WorkflowResult(
        reply=reply,
        intent=agent.name,
        reason=decision.reason,
    )