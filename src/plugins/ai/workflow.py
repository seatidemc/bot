"""AI 消息工作流：选择 agent → agent loop → 最终回复。"""

from __future__ import annotations

from dataclasses import dataclass

from . import agents


@dataclass(frozen=True)
class WorkflowResult:
    reply: str
    intent: str


async def run(
    intent: str, text: str, history: list[dict[str, str]]
) -> WorkflowResult:
    """用指定 agent 执行一次完整的 AI 对话。

    intent 由调用方决定：普通群消息为 chat，/提问 为 knowledge。
    chat / knowledge 各自有独立的系统提示词、模型配置和工具集。
    """
    agent = agents.get_agent(intent)
    reply = await agents.run_agent(agent, text, history)
    return WorkflowResult(reply=reply, intent=agent.name)
