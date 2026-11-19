"""DeepSeek Chat Completions API 封装（OpenAI 兼容协议）。"""

from __future__ import annotations

import httpx

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

    return content.strip()
