"""DeepSeek Chat Completions API 封装（OpenAI 兼容协议）。"""

from __future__ import annotations

import httpx
from nonebot.log import logger

from . import config


class DeepSeekError(Exception):
    """DeepSeek 请求失败。"""

    def __init__(self, message: str, status: int | None = None):
        self.status = status
        self.message = message
        super().__init__(message)


async def chat(messages: list[dict[str, str]]) -> str:
    """调用 DeepSeek 补全，返回 assistant 的文本内容。"""
    if not config.DEEPSEEK_API_KEY:
        raise DeepSeekError("未配置 DEEPSEEK_API_KEY")

    url = f"{config.DEEPSEEK_BASE_URL}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.DEEPSEEK_MODEL,
        "messages": messages,
        "stream": False,
        "temperature": config.AI_TEMPERATURE,
        "max_tokens": config.AI_MAX_TOKENS,
        "reasoning_effort": config.AI_THINKING_EFFORT,
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(config.AI_TIMEOUT)
        ) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as e:
        raise DeepSeekError("请求超时，请稍后再试") from e
    except httpx.HTTPError as e:
        raise DeepSeekError(f"网络错误：{e}") from e

    if resp.status_code != 200:
        msg = resp.text[:200]
        try:
            body = resp.json()
            msg = (body.get("error") or {}).get("message") or msg
        except Exception:
            pass
        if resp.status_code == 401:
            msg = "API Key 无效，请检查配置中的 DEEPSEEK_API_KEY"
        elif resp.status_code == 402:
            msg = "DeepSeek 账户余额不足"
        elif resp.status_code == 429:
            msg = "请求过于频繁，请稍后再试"
        elif resp.status_code >= 500:
            msg = "DeepSeek 服务端异常，请稍后再试"
        raise DeepSeekError(msg, resp.status_code)

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise DeepSeekError(f"响应解析失败：{resp.text[:200]}") from e

    _log_usage(data.get("usage"))
    return content.strip()


def _log_usage(usage: dict | None) -> None:
    """记录本次调用的 token 用量，便于观察成本构成。"""
    if not isinstance(usage, dict):
        return

    details = usage.get("completion_tokens_details") or {}
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
        f"DeepSeek 用量：prompt {prompt}（{cache}），"
        f"completion {completion}（思考 {reasoning or 0}），total {total}"
    )
