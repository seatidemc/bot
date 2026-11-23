"""AI 消息工作流：router 选择 agent → agent loop → 最终回复。"""

from __future__ import annotations

from dataclasses import dataclass

from . import agents, chat_log, router


@dataclass(frozen=True)
class WorkflowResult:
    reply: str
    intent: str
    reason: str


async def run(
    text: str, history: list[chat_log.ChatLogEntry]
) -> WorkflowResult:
    """路由并执行一次完整的 AI 对话。

    chat / knowledge 各自有独立的系统提示词、模型配置和工具集。
    """
    decision = await router.route(text, history)
    agent = agents.get_agent(decision.intent)
    reply = await agents.run_agent(agent, text, history)
    return WorkflowResult(
        reply=reply,
        intent=agent.name,
        reason=decision.reason,
    )