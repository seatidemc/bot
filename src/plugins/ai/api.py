"""OpenAI 兼容 Chat Completions 客户端，支持工具调用。

router / chat / knowledge / invalid 共用本模块，通过传入不同的
``ModelConfig`` 决定端点、模型 id、temperature、thinking effort 等。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from nonebot.log import logger

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


def _completions_url(base_url: str) -> str:
    """允许配置完整端点，也允许只配到 /v1 或域名。"""
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url.rstrip('/')}/chat/completions"


def _error_message(resp: httpx.Response) -> str:
    text = resp.text[:300]
    try:
        body = resp.json()
    except ValueError:
        return text

    if not isinstance(body, dict):
        return text

    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or text)
    if isinstance(error, str):
        return error
    for key in ("message", "detail", "details"):
        value = body.get(key)
        if value:
            return str(value)
    return text


def _validate_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        raise ValueError("messages 不能为空")


async def chat_completion(
    model: ModelConfig,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = "auto",
    response_format: dict[str, Any] | None = None,
) -> Completion:
    """调用一次 Chat Completions，返回文本和工具调用请求。"""
    _validate_messages(messages)

    if not model.api_key:
        raise LLMError(
            "未配置 API Key",
            component=model.component,
            model=model.model,
        )

    payload: dict[str, Any] = {
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
        payload["reasoning_effort"] = model.thinking_effort
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    if response_format:
        payload["response_format"] = response_format

    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
    }
    url = _completions_url(model.base_url)

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(model.timeout)
        ) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as e:
        raise LLMError(
            "请求超时，请稍后再试",
            component=model.component,
            model=model.model,
        ) from e
    except httpx.HTTPError as e:
        raise LLMError(
            f"网络错误：{e.__class__.__name__}",
            component=model.component,
            model=model.model,
        ) from e

    if resp.status_code != 200:
        msg = _error_message(resp)
        if resp.status_code == 401:
            msg = f"API Key 无效，请检查 {model.component} 模型的配置"
        elif resp.status_code == 402:
            msg = "账户余额不足"
        elif resp.status_code == 429:
            msg = "请求过于频繁，请稍后再试"
        elif resp.status_code >= 500:
            msg = "模型服务端异常，请稍后再试"
        raise LLMError(
            msg,
            status=resp.status_code,
            component=model.component,
            model=model.model,
        )

    try:
        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message") or {}
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise LLMError(
            f"响应解析失败：{resp.text[:200]}",
            status=resp.status_code,
            component=model.component,
            model=model.model,
        ) from e

    content = message.get("content")
    if not isinstance(content, str):
        content = "" if content is None else str(content)

    tool_calls = message.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []

    _log_usage(model, data.get("usage"))
    finish_reason = choice.get("finish_reason")
    return Completion(
        content=content.strip(),
        tool_calls=tool_calls,
        raw_message=message,
        finish_reason=str(finish_reason or ""),
    )


def _log_usage(model: ModelConfig, usage: Any) -> None:
    """记录本次调用的 token 用量，便于观察成本构成。"""
    if not isinstance(usage, dict):
        return

    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    prompt = usage.get("prompt_tokens")
    hit = usage.get("prompt_cache_hit_tokens")
    miss = usage.get("prompt_cache_miss_tokens")
    completion = usage.get("completion_tokens")
    reasoning = details.get("reasoning_tokens")
    total = usage.get("total_tokens")

    if hit is None and miss is None:
        cache = "缓存信息缺失"
    else:
        cache = f"缓存命中 {hit or 0} / 未命中 {miss or 0}"

    logger.info(
        f"LLM 用量[{model.component}/{model.model}]："
        f"prompt {prompt}（{cache}），"
        f"completion {completion}（思考 {reasoning or 0}），total {total}"
    )