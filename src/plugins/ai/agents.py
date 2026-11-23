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

from . import api, chat_log, config, media
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


# 群聊记录作为第一条 user 消息注入，而不是拼进 system prompt：
# 每条记录都能带上发言人和时间，同时 system prompt 保持稳定，便于
# provider 侧做 prompt 缓存。当前消息单独成一条 user 消息，同样带发言人。
_HISTORY_HEADER = "# 最近聊天记录"
_CURRENT_HEADER = "# 当前消息"


def _content_with_parts(
    text: str,
    parts: list[dict[str, Any]],
    notes: list[str],
) -> str | list[dict[str, Any]]:
    """没有媒体时仍用普通字符串 content；有媒体时拼成多模态数组。

    ``notes`` 是哪些媒体没附带上的说明（体积超限、文件丢失），放在文本块
    末尾，避免模型以为占位标记对应的图片一定看得到。
    """
    body = text
    if notes:
        body = f"{body}\n\n（{'；'.join(notes)}）"
    if not parts:
        # 媒体全部没带上时也要把原因告诉模型，避免它假装看过图
        return body
    return [{"type": "text", "text": body}, *parts]


def _build_history_message(
    history: list[chat_log.ChatLogEntry],
    parts: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    transcript = "\n".join(
        chat_log.format_transcript_line(entry) for entry in history
    )
    return {
        "role": "user",
        "content": _content_with_parts(
            f"{_HISTORY_HEADER}\n\n{transcript}",
            parts or [],
            notes or [],
        ),
    }


def _build_current_message(
    current: chat_log.ChatLogEntry,
    parts: list[dict[str, Any]] | None = None,
    notes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "role": "user",
        "content": _content_with_parts(
            f"{_CURRENT_HEADER}\n\n"
            f"{chat_log.format_transcript_line(current)}",
            parts or [],
            notes or [],
        ),
    }


async def _media_messages(
    current: chat_log.ChatLogEntry,
    history: list[chat_log.ChatLogEntry],
) -> tuple[
    dict[str, Any], dict[str, Any] | None
]:
    """构造带媒体的当前消息 + 历史消息。

    当前消息的媒体总是优先附带；历史消息只取最近
    ``AI_MEDIA_CONTEXT_MESSAGES`` 条里出现的媒体，总数不超过
    ``AI_MEDIA_MAX_CONTEXT_ITEMS``。返回（当前消息, 历史消息或 None）。
    """
    if not config.AI_MEDIA_ENABLED:
        return _build_current_message(current), (
            _build_history_message(history) if history else None
        )

    budget = config.AI_MEDIA_MAX_CONTEXT_BYTES
    current_parts, current_notes, used = await media.build_parts(
        current.media, config.AI_MEDIA_MAX_CONTEXT_ITEMS, budget
    )
    current_message = _build_current_message(
        current, current_parts, current_notes
    )

    history_items: list[media.MediaItem] = []
    if config.AI_MEDIA_CONTEXT_MESSAGES > 0 and history:
        for entry in history[-config.AI_MEDIA_CONTEXT_MESSAGES:]:
            history_items.extend(entry.media)

    history_message = None
    if history:
        remaining = max(
            0, config.AI_MEDIA_MAX_CONTEXT_ITEMS - len(current_parts)
        )
        history_parts, history_notes, _ = await media.build_parts(
            history_items, remaining, max(0, budget - used)
        )
        history_message = _build_history_message(
            history, history_parts, history_notes
        )
    return current_message, history_message


async def run_agent(
    agent: Agent,
    current: chat_log.ChatLogEntry,
    history: list[chat_log.ChatLogEntry],
) -> str:
    current_message, history_message = await _media_messages(
        current, history
    )

    messages: list[dict[str, Any]] = []
    if agent.model.system_prompt:
        messages.append(
            {"role": "system", "content": agent.model.system_prompt}
        )
    if history_message is not None:
        messages.append(history_message)
    messages.append(current_message)

    empty_replies = 0
    media_fallback = False
    while True:
        try:
            result = await api.chat_completion(
                agent.model,
                messages,
                tools=agent.tool_schemas(),
                tool_choice="auto",
            )
        except api.LLMError as e:
            # 配置的模型可能不支持 image_url / video_url；首次 400 时去掉
            # 媒体 part 用纯文本重试一次，至少还能基于占位标记回复。
            if (
                not media_fallback
                and e.status == 400
                and media.has_media_parts(messages)
            ):
                media_fallback = True
                logger.warning(
                    f"[{agent.name}] 模型拒绝多模态内容（400），"
                    "改为纯文本重试"
                )
                messages = media.strip_media_parts(messages)
                continue
            raise

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