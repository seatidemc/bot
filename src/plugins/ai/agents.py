"""两个 agent：普通聊天 / 知识库问答。

每个 agent 拥有独立的模型配置、系统提示词和工具集；这里实现一个
基于 openai SDK 的 Chat Completions 工具调用循环（agent loop）。
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
_AGENTS = {
    agent.name: agent for agent in (CHAT_AGENT, KNOWLEDGE_AGENT)
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


def _format_tool_args(raw_args: Any) -> str:
    if raw_args is None:
        return "{}"
    if isinstance(raw_args, str):
        return raw_args.strip() or "{}"
    try:
        return json.dumps(raw_args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(raw_args)


async def _run_tool_call(agent: Agent, call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") or {}
    name = str(function.get("name") or "")
    tool_id = str(call.get("id") or "")
    raw_args = function.get("arguments")

    # 调用痕迹只记录工具名和传入参数，不记录工具返回
    logger.info(
        f"[{agent.name}] 工具调用：{name or '(未提供名称)'}，"
        f"参数 {_format_tool_args(raw_args)}"
    )

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


# 模型偶发只返回空正文、finish_reason 仍是 stop（服务端抖动），
# 与提示词和参数无关；原样重试有限次数，仍为空则交给上层兜底。
_EMPTY_REPLY_RETRIES = 2


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

    empty_replies = 0
    while True:
        result = await api.chat_completion(
            agent.model,
            messages,
            tools=agent.tool_schemas(),
            tool_choice="auto",
        )

        if result.tool_calls:
            messages.append(result.assistant_message())
            messages.extend(await _run_tool_calls(agent, result.tool_calls))
            empty_replies = 0
            continue

        if result.content:
            return result.content

        empty_replies += 1
        logger.warning(
            f"[{agent.name}] 模型返回空正文"
            f"（finish_reason={result.finish_reason or '未知'}，"
            f"第 {empty_replies} 次）"
        )
        if empty_replies > _EMPTY_REPLY_RETRIES:
            return ""