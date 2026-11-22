"""router 与两个 agent：普通聊天 / 知识库问答。

每个 agent 拥有独立的模型配置、系统提示词和工具集；这里实现一个
简单的 OpenAI 兼容工具调用循环（agent loop）。
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
_AGENTS = {agent.name: agent for agent in (CHAT_AGENT, KNOWLEDGE_AGENT)}


def get_agent(name: str) -> Agent:
    return _AGENTS[name]


# ─── router ──────────────────────────────────────────────────


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    reason: str = ""


_INTENTS = ("chat", "knowledge", "invalid")


def _parse_route(content: str, finish_reason: str = "") -> RouteDecision:
    """严格解析 router 输出：只接受合法 JSON，intent 必须是固定枚举值。"""

    def fail(message: str):
        raise api.LLMError(
            message,
            component="router",
            model=config.ROUTER.model,
        )

    text = content.strip()
    if not text:
        fail(
            "router 返回空内容"
            f"（finish_reason={finish_reason or '未知'}）"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fail(f"router 输出不是合法 JSON：{content[:120]!r}")

    if not isinstance(data, dict):
        fail(f"router 输出不是 JSON 对象：{content[:120]!r}")

    intent = data.get("intent")
    if intent not in _INTENTS:
        fail(f"router 返回未知 intent：{intent!r}")

    reason = str(data.get("reason") or "").strip()[:120]
    return RouteDecision(intent, reason)


async def route(text: str, history: list[dict[str, str]]) -> RouteDecision:
    messages: list[dict[str, Any]] = []
    if config.ROUTER.system_prompt:
        messages.append(
            {"role": "system", "content": config.ROUTER.system_prompt}
        )

    limit = config.AI_ROUTER_HISTORY_TURNS * 2
    if limit > 0 and history:
        messages.extend(history[-limit:])
    messages.append({"role": "user", "content": text})

    result = await api.chat_completion(
        config.ROUTER,
        messages,
        response_format={"type": "json_object"},
    )
    decision = _parse_route(result.content, result.finish_reason)
    reason = f"（{decision.reason}）" if decision.reason else ""
    logger.info(f"AI 路由：{decision.intent}{reason}")
    return decision


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