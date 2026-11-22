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

from . import api, config

# ─── 分类问题 ────────────────────────────────────────────────

_INTENTS = ("chat", "knowledge")

# criteria 的 key 就是最终 intent；描述帮助模型区分两类消息
_ROUTER_CRITERIA: dict[str, str] = {
    "chat": (
        "日常闲聊、打招呼、情绪表达、与 Seatide 服务器无关的通用问题、"
        "乱码或没有实际意义的内容"
    ),
    "knowledge": (
        "询问 Seatide 服务器的规则、玩法、插件、指令、公告、历史、"
        "常见问题等，需要查阅知识库才能准确回答的问题，以及需要查询"
        "实时服务器状态或当前时间的问题"
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


def _build_state(
    text: str, history: list[dict[str, str]]
) -> dict[str, Any]:
    limit = config.ROUTER.history_turns * 2
    recent = history[-limit:] if limit > 0 and history else []
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
    text: str, history: list[dict[str, str]]
) -> RouteDecision:
    """调用 System One 判断最新一条消息应该交给哪个 agent。"""
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

    intent = answer.choice
    if intent not in _INTENTS:
        raise api.LLMError(
            f"router 返回未知 intent：{intent!r}",
            component="router",
            model=config.ROUTER.model,
        )

    confidence = answer.confidence
    reason = (
        f"confidence={confidence:.2f}"
        if confidence is not None
        else ""
    )
    probabilities = dict(answer.probabilities or {})
    logger.debug(f"router 概率：{probabilities}")
    logger.info(
        f"AI 路由：{intent}" + (f"（{reason}）" if reason else "")
    )
    return RouteDecision(
        intent=intent,
        reason=reason,
        confidence=confidence,
        probabilities=probabilities,
    )