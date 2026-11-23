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
    current: chat_log.ChatLogEntry,
    history: list[chat_log.ChatLogEntry],
) -> WorkflowResult:
    """路由并执行一次完整的 AI 对话。

    chat / knowledge 各自有独立的系统提示词、模型配置和工具集。
    ``current`` 是本次要回复的群消息（带发言人和时间），``history`` 是
    群里最近的聊天记录；router 只用正文，两个 agent 两者都会拿到。
    """
    decision = await router.route(current.content, history)
    agent = agents.get_agent(decision.intent)
    reply = await agents.run_agent(agent, current, history)
    return WorkflowResult(
        reply=reply,
        intent=agent.name,
        reason=decision.reason,
    )