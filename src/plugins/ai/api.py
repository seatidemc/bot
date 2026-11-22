"""OpenAI Chat Completions 客户端（官方 openai SDK）。

chat / knowledge / invalid 共用本模块，固定使用 ``openai-completions``
格式，由传入的 ``ModelConfig`` 决定端点、模型 id、temperature、thinking
effort 等。router 使用 TypeSafe System One，不经过本模块，见 ``router.py``。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from nonebot import get_driver
from nonebot.log import logger
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from .config import ModelConfig


class LLMError(Exception):
    """模型请求失败。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        component: str = "",
        model: str = "",
    ):
        self.status = status
        self.message = message
        self.component = component
        self.model = model
        prefix = f"[{component}] " if component else ""
        super().__init__(f"{prefix}{message}")


@dataclass
class Completion:
    """一次模型调用的结果。"""

    content: str
    tool_calls: list[dict[str, Any]]
    raw_message: dict[str, Any]
    finish_reason: str = ""

    def assistant_message(self) -> dict[str, Any]:
        """构造可回传给模型的历史 assistant 消息（保留思考内容）。"""
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.raw_message.get("content"),
        }
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        # DeepSeek reasoner 在工具调用循环中要求把 reasoning_content 回传
        reasoning = self.raw_message.get("reasoning_content")
        if reasoning:
            message["reasoning_content"] = reasoning
        return message


# ─── SDK 客户端 ──────────────────────────────────────────────


def _normalize_base_url(base_url: str) -> str:
    """把配置里的端点转换为 SDK 需要的 API 根地址。

    SDK 会自动拼接 ``/chat/completions``，所以旧配置里写到完整
    ``.../chat/completions`` 的端点要先把后缀去掉。
    """
    url = base_url.rstrip("/")
    suffix = "/chat/completions"
    if url.endswith(suffix):
        url = url[: -len(suffix)]
    return url


_clients: dict[tuple[str, str, float], AsyncOpenAI] = {}
_clients_lock = asyncio.Lock()


async def _get_client(model: ModelConfig) -> AsyncOpenAI:
    """按端点 / Key / 超时复用客户端，避免每次都重建连接池。"""
    key = (model.base_url, model.api_key, model.timeout)
    client = _clients.get(key)
    if client is not None:
        return client

    async with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = AsyncOpenAI(
                api_key=model.api_key,
                base_url=_normalize_base_url(model.base_url),
                timeout=model.timeout,
                # 与重构前保持一致：单次请求、不做 SDK 层自动重试
                max_retries=0,
            )
            _clients[key] = client
    return client


@get_driver().on_shutdown
async def _close_clients() -> None:
    for client in _clients.values():
        await client.close()
    _clients.clear()


# ─── 错误映射 ────────────────────────────────────────────────


def _error_text(exc: APIStatusError) -> str:
    """尽量从响应体里取出服务端给的错误说明。"""
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            text = error.get("message")
            if text:
                return str(text)
        elif isinstance(error, str) and error:
            return error
        for key in ("message", "detail", "details"):
            value = body.get(key)
            if value:
                return str(value)
    elif isinstance(body, str) and body.strip():
        return body[:300]
    return (exc.message or "")[:300]


def _llm_error(exc: APIError, model: ModelConfig) -> LLMError:
    if isinstance(exc, APITimeoutError):
        message = "请求超时，请稍后再试"
        status = None
    elif isinstance(exc, APIConnectionError):
        message = f"网络错误：{exc.__class__.__name__}"
        status = None
    elif isinstance(exc, APIStatusError):
        status = exc.status_code
        if status == 401:
            message = f"API Key 无效，请检查 {model.component} 模型的配置"
        elif status == 402:
            message = "账户余额不足"
        elif status == 429:
            message = "请求过于频繁，请稍后再试"
        elif status >= 500:
            message = "模型服务端异常，请稍后再试"
        else:
            message = _error_text(exc)
    else:
        message = f"请求失败：{exc.__class__.__name__}"
        status = None

    return LLMError(
        message,
        status=status,
        component=model.component,
        model=model.model,
    )


# ─── 调用 ────────────────────────────────────────────────────


def _validate_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        raise ValueError("messages 不能为空")


async def chat_completion(
    model: ModelConfig,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = "auto",
) -> Completion:
    """调用一次 Chat Completions，返回文本和工具调用请求。"""
    _validate_messages(messages)

    if not model.api_key:
        raise LLMError(
            "未配置 API Key",
            component=model.component,
            model=model.model,
        )

    kwargs: dict[str, Any] = {
        "model": model.model,
        "messages": messages,
        "stream": False,
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
    }
    # 关闭思考时不发 reasoning_effort 字段。DeepSeek 在
    # reasoning_effort="none" + JSON 模式下会偶发只返回空白内容
    # （finish_reason=stop，和 temperature、max_tokens 无关）。
    if model.thinking_effort != "none":
        kwargs["extra_body"] = {"reasoning_effort": model.thinking_effort}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice or "auto"

    client = await _get_client(model)
    try:
        response = await client.chat.completions.create(**kwargs)
    except APIError as e:
        raise _llm_error(e, model) from e

    try:
        choice = response.choices[0]
        message = choice.message
    except (AttributeError, IndexError, TypeError) as e:
        raise LLMError(
            "响应解析失败：模型没有返回有效内容",
            component=model.component,
            model=model.model,
        ) from e

    content = message.content
    if not isinstance(content, str):
        content = "" if content is None else str(content)

    raw_message = message.model_dump(exclude_none=True)
    tool_calls = raw_message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []

    _log_usage(model, response.usage)
    return Completion(
        content=content.strip(),
        tool_calls=tool_calls,
        raw_message=raw_message,
        finish_reason=str(choice.finish_reason or ""),
    )


def _usage_get(usage: Any, key: str) -> Any:
    """读取 usage 字段：SDK 对象优先取属性，再取 provider 扩展字段。"""
    if isinstance(usage, dict):
        return usage.get(key)
    value = getattr(usage, key, None)
    if value is not None:
        return value
    extra = getattr(usage, "model_extra", None) or {}
    return extra.get(key)


def _log_usage(model: ModelConfig, usage: Any) -> None:
    """记录本次调用的 token 用量，便于观察成本构成。"""
    if usage is None:
        return

    prompt = _usage_get(usage, "prompt_tokens")
    completion = _usage_get(usage, "completion_tokens")
    total = _usage_get(usage, "total_tokens")

    details = _usage_get(usage, "completion_tokens_details")
    if isinstance(details, dict):
        reasoning = details.get("reasoning_tokens")
    else:
        reasoning = getattr(details, "reasoning_tokens", None)

    # DeepSeek 直接给出命中 / 未命中；OpenAI 风格只给 prompt_tokens_details.cached_tokens
    hit = _usage_get(usage, "prompt_cache_hit_tokens")
    miss = _usage_get(usage, "prompt_cache_miss_tokens")
    if hit is None and miss is None:
        prompt_details = _usage_get(usage, "prompt_tokens_details")
        if isinstance(prompt_details, dict):
            cached = prompt_details.get("cached_tokens")
        else:
            cached = getattr(prompt_details, "cached_tokens", None)
        if cached is not None and isinstance(prompt, int):
            hit, miss = cached, max(0, prompt - cached)

    if hit is None and miss is None:
        cache = "缓存信息缺失"
    else:
        cache = f"缓存命中 {hit or 0} / 未命中 {miss or 0}"

    logger.info(
        f"LLM 用量[{model.component}/{model.model}]："
        f"prompt {prompt}（{cache}），"
        f"completion {completion}（思考 {reasoning or 0}），total {total}"
    )