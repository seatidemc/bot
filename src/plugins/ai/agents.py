"""三个 agent：普通聊天 / 知识库问答 / 无意义消息回应。

每个 agent 拥有独立的模型配置、系统提示词和工具集；这里实现一个
基于 openai SDK 的 Chat Completions 工具调用循环（agent loop）。invalid
agent 不使用工具，只负责对 router 判为无意义的消息生成一句自然回应。
router 本身在 ``router.py``，通过 TypeSafe System One 分类。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from nonebot.log import logger

from . import api, config
from .tools import CHAT_TOOLS, KNOWLEDGE_TOOLS, Tool


@dataclass(frozen=True)
class Agent:
    name: str
    model: config.ModelConfig
    tools: tuple[Tool, ...]

    @property
    def tool_map(self) -> dict[str, Tool]:
        return {tool.name: tool for tool in self.tools}

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.tools]


CHAT_AGENT = Agent(name="chat", model=config.CHAT, tools=CHAT_TOOLS)
KNOWLEDGE_AGENT = Agent(
    name="knowledge", model=config.KNOWLEDGE, tools=KNOWLEDGE_TOOLS
)
INVALID_AGENT = Agent(name="invalid", model=config.INVALID, tools=())
_AGENTS = {
    agent.name: agent
    for agent in (CHAT_AGENT, KNOWLEDGE_AGENT, INVALID_AGENT)
}


def get_agent(name: str) -> Agent:
    return _AGENTS[name]


# ─── agent loop ──────────────────────────────────────────────


def _tool_message(tool_id: str, content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": tool_id,
        "content": str(content),
    }


async def _run_tool_call(agent: Agent, call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    tool_id = str(call.get("id") or "")
    raw_args = function.get("arguments")

    if isinstance(raw_args, str):
        try:
            arguments = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            return _tool_message(tool_id, f"工具参数不是合法 JSON：{e}")
    elif isinstance(raw_args, dict):
        arguments = raw_args
    elif raw_args is None:
        arguments = {}
    else:
        return _tool_message(tool_id, "工具参数格式无效")

    if not isinstance(arguments, dict):
        return _tool_message(tool_id, "工具参数必须是 JSON 对象")

    tool = agent.tool_map.get(name)
    if tool is None:
        return _tool_message(tool_id, f"未知工具：{name}")

    logger.debug(f"[{agent.name}] 调用工具 {name}，参数 {arguments}")
    try:
        content = await tool.handler(**arguments)
    except Exception as e:
        logger.warning(f"[{agent.name}] 工具 {name} 执行失败: {e}")
        content = f"工具 {name} 执行失败：{e.__class__.__name__}: {e}"
    return _tool_message(tool_id, content)


async def _run_tool_calls(
    agent: Agent, calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    valid_calls = [call for call in calls if isinstance(call, dict)]
    if not valid_calls:
        return []
    return await asyncio.gather(
        *(_run_tool_call(agent, call) for call in valid_calls)
    )


async def run_agent(
    agent: Agent, text: str, history: list[dict[str, str]]
) -> str:
    messages: list[dict[str, Any]] = []
    if agent.model.system_prompt:
        messages.append(
            {"role": "system", "content": agent.model.system_prompt}
        )
    messages.extend(history)
    messages.append({"role": "user", "content": text})

    while True:
        result = await api.chat_completion(
            agent.model,
            messages,
            tools=agent.tool_schemas(),
            tool_choice="auto",
        )

        if not result.tool_calls:
            return result.content

        messages.append(result.assistant_message())
        messages.extend(await _run_tool_calls(agent, result.tool_calls))