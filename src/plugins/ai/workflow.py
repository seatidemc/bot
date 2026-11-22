"""AI 消息工作流：router 选择 agent → agent loop → 最终回复。"""

from __future__ import annotations

from dataclasses import dataclass

from . import agents

# router 判定消息没有意义时不进入任何 agent，直接回复这句话
INVALID_REPLY = "似乎这并不是个问题哦，有问题的时候再来找我吧"


@dataclass(frozen=True)
class WorkflowResult:
    reply: str
    intent: str
    reason: str


async def run(text: str, history: list[dict[str, str]]) -> WorkflowResult:
    """路由并执行一次完整的 AI 对话。"""
    decision = await agents.route(text, history)
    if decision.intent == "invalid":
        return WorkflowResult(
            reply=INVALID_REPLY,
            intent=decision.intent,
            reason=decision.reason,
        )
    agent = agents.get_agent(decision.intent)
    reply = await agents.run_agent(agent, text, history)
    return WorkflowResult(
        reply=reply,
        intent=agent.name,
        reason=decision.reason,
    )