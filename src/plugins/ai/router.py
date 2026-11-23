"""router：调用 TypeSafe System One 把消息分成 chat / knowledge。

System One 不是对话模型：输入一段 state 和若干结构化问题，返回带概率的
分类答案。这里用一个 ``Choice`` 问题让模型在两个 agent 中选一个，最近
对话和最新消息一起放进 state，用于理解指代和追问。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nonebot import get_driver
from nonebot.log import logger
from typesafe_sdk import (
    AsyncTypeSafeClient,
    Choice,
    RetryPolicy,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from ..lab_client import is_command_text
from . import api, chat_log, config, keywords

# ─── 分类问题 ────────────────────────────────────────────────

_INTENTS = ("chat", "knowledge")

# criteria 的 key 就是最终 intent；描述帮助模型区分两类消息
_ROUTER_CRITERIA: dict[str, str] = {
    "chat": (
        "日常闲聊、打招呼、情绪表达、与 Seatide 服务器无关的通用问题、"
        "乱码、网络梗或没有实际意义的内容"
    ),
    "knowledge": (
        "询问 Seatide 服务器的规则、玩法、插件、指令、公告、历史、"
        "常见问题、陌生Minecraft内容等需要查阅知识库、外部网络信息的问题，或与服务器相关的读写操作"
    ),
}


@dataclass(frozen=True)
class RouteDecision:
    intent: str
    reason: str = ""
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)


# ─── SDK 客户端 ──────────────────────────────────────────────

_client: AsyncTypeSafeClient | None = None
_client_lock = asyncio.Lock()


async def _get_client() -> AsyncTypeSafeClient:
    """复用同一个 SDK 客户端，避免每次路由都重新建立 TLS 连接。"""
    global _client
    if _client is None:
        async with _client_lock:
            if _client is None:
                _client = AsyncTypeSafeClient(
                    api_key=config.ROUTER.api_key,
                    base_url=config.ROUTER.base_url,
                    model=config.ROUTER.model,
                    timeout=config.ROUTER.timeout,
                    retry=RetryPolicy(
                        max_retries=config.ROUTER.max_retries,
                        timeout=config.ROUTER.timeout,
                    ),
                    # SDK 默认对 ≥1024 字节的请求体做 zstd 压缩，但阿里云百炼
                    # 的 System One 端点不识别 Content-Encoding，会把 zstd 帧
                    # 直接当 JSON 解码：zstd 魔数首字节 0x28 即 '('，于是报
                    # "invalid character '(' looking for beginning of value"。
                    # 关闭压缩后请求体大小不再受 1024 字节限制。
                    compression=False,
                )
    return _client


@get_driver().on_shutdown
async def _close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ─── 错误映射 ────────────────────────────────────────────────


def _llm_error(exc: Exception) -> api.LLMError:
    """把 SDK 异常转换成现有的用户可见错误。"""
    if isinstance(exc, TypeSafeAuthenticationError):
        message = "API Key 无效，请检查 router 模型的配置"
    elif isinstance(exc, TypeSafePermissionDeniedError):
        message = "API Key 无权访问 System One API"
    elif isinstance(exc, TypeSafeRateLimitError):
        message = "请求过于频繁，请稍后再试"
    elif isinstance(exc, TypeSafeAPITimeoutError):
        message = "请求超时，请稍后再试"
    elif isinstance(exc, TypeSafeAPIConnectionError):
        message = "网络错误，无法连接 System One API"
    elif isinstance(exc, TypeSafeAPIResponseValidationError):
        message = f"响应解析失败：{exc.field_path} 字段无效"
    elif isinstance(exc, TypeSafeAPIError):
        if exc.status >= 500:
            message = "System One 服务端异常，请稍后再试"
        else:
            message = str(exc)[:300]
    elif isinstance(exc, TypeSafeError):
        message = str(exc)[:300]
    else:
        message = f"{exc.__class__.__name__}: {exc}"

    return api.LLMError(
        message,
        status=getattr(exc, "status", None),
        component="router",
        model=config.ROUTER.model,
    )


# ─── 路由 ────────────────────────────────────────────────────


def _keyword_decision(text: str) -> RouteDecision | None:
    """命中关键词词表时直接判为 knowledge，跳过 System One。"""
    rule = keywords.match(text, config.ROUTER_KEYWORDS)
    if rule is None:
        return None

    logger.info(
        f"AI 路由：knowledge（命中关键词 {rule.pattern!r}，"
        "跳过 System One）"
    )
    return RouteDecision(
        intent="knowledge",
        reason=f"关键词命中 {rule.pattern!r}",
    )


def _build_state(
    text: str, history: list[chat_log.ChatLogEntry]
) -> dict[str, Any]:
    """构造 System One 的 state。

    System One 对整个 state 打分，而不是只判断 ``latest_message``。bot 自己
    的回复（尤其是 knowledge agent 的长回答）会让「你好」「？」这类短消息
    被判成 knowledge，所以 router 只看最近的用户消息，并跳过斜杠指令：
    指令本身不进入 AI，却可能带着 ``/状态`` 这样的知识型关键词。

    保留最近 ``AI_ROUTER_HISTORY_TURNS`` 条用户消息仅用于理解追问和指代；
    再多会被无关闲聊稀释。
    """
    limit = config.ROUTER.history_turns
    if limit <= 0:
        recent: list[dict[str, str]] = []
    else:
        recent = [
            {
                "role": "user",
                "content": entry.content,
            }
            for entry in history
            if entry.role == "user"
            and not is_command_text(entry.content)
        ][-limit:]
    return {
        "recent_conversation": recent,
        "latest_message": text,
    }


def _log_usage(result: SystemOneResponse) -> None:
    usage = result.usage
    prompt = usage.input_tokens
    completion = usage.output_tokens
    if prompt is None and completion is None:
        return
    parts = []
    if prompt is not None:
        parts.append(f"prompt {prompt}")
    if completion is not None:
        parts.append(f"completion {completion}")
    if isinstance(prompt, int) and isinstance(completion, int):
        parts.append(f"total {prompt + completion}")
    logger.info(
        f"LLM 用量[router/{result.model}]：" + "，".join(parts)
    )


async def route(
    text: str, history: list[chat_log.ChatLogEntry]
) -> RouteDecision:
    """判断最新一条消息应该交给哪个 agent。

    先查关键词词表，命中即直接返回 knowledge；否则调用 System One。
    """
    keyword_decision = _keyword_decision(text)
    if keyword_decision is not None:
        return keyword_decision

    questions = {"intent": Choice(criteria=_ROUTER_CRITERIA)}

    try:
        client = await _get_client()
        result = await client.system_one(_build_state(text, history), questions)
    except TypeSafeError as e:
        raise _llm_error(e) from e

    _log_usage(result)

    answer = result.choices.get("intent")
    if answer is None:
        raise api.LLMError(
            "router 没有返回 choice 类型的 intent 答案",
            component="router",
            model=config.ROUTER.model,
        )

    raw_intent = answer.choice
    if raw_intent not in _INTENTS:
        raise api.LLMError(
            f"router 返回未知 intent：{raw_intent!r}",
            component="router",
            model=config.ROUTER.model,
        )

    confidence = answer.confidence
    probabilities = dict(answer.probabilities or {})

    # knowledge 概率不足时按 chat 处理。这里不看 confidence：System One
    # 对 knowledge 的 confidence 普遍偏低（概率 0.70 时也只有 0.40 左右），
    # 用它当门槛会把正常知识问题也挡掉。门槛可配，0 表示不启用。
    intent = raw_intent
    override = ""
    threshold = config.ROUTER.min_knowledge_probability
    knowledge_prob = probabilities.get("knowledge")
    if (
        intent == "knowledge"
        and threshold > 0
        and knowledge_prob is not None
        and knowledge_prob < threshold
    ):
        intent = "chat"
        override = (
            f"，knowledge 概率 {knowledge_prob:.2f} 低于门槛 "
            f"{threshold:.2f}，按 chat 处理"
        )

    reason = (
        f"confidence={confidence:.2f}"
        if confidence is not None
        else ""
    )
    distribution = "，".join(
        f"{name}={value:.2f}"
        for name, value in sorted(
            probabilities.items(), key=lambda item: item[1], reverse=True
        )
    )
    logger.info(
        f"AI 路由：{intent}"
        + (f"（{reason}）" if reason else "")
        + override
        + (f"，概率 {distribution}" if distribution else "")
    )
    if probabilities:
        top = max(probabilities, key=probabilities.__getitem__)
        if top != raw_intent:
            logger.warning(
                f"router 返回的 choice={raw_intent} 不是概率最高项（{top}），"
                f"概率 {distribution}"
            )
    return RouteDecision(
        intent=intent,
        reason=reason,
        confidence=confidence,
        probabilities=probabilities,
    )