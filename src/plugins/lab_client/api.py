"""封装对 go-aliyunmc-v2 机器人接口的 HTTP 调用。

`BACKEND_BASE_URL` 是完整前缀（默认含 `/bot-v1`），API 路径直接拼在它后面，
代码不再自动追加任何路径段。

后端只接受来自本机回环地址的直连请求，不需要 token / 签名，
响应统一为 `{"data": ...}` / `{"details": "..."}`。
"""

from __future__ import annotations

from typing import Any

import httpx

from . import config

_STATUS_HINTS = {
    403: "后端拒绝访问：请确认 bot 与后端在同一台机器，并通过 127.0.0.1 直连（不能经过反向代理）",
    409: "已有部署链任务正在执行（创建/部署/启动），请等它结束后再试",
    429: "后端请求过于频繁，请稍后再试",
}


class APIError(Exception):
    """后端返回了非 2xx 状态码或无法解析的响应。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


def _url(path: str) -> str:
    return f"{config.BACKEND_BASE_URL}{path}"


async def _request(method: str, path: str) -> dict[str, Any]:
    """发起请求，返回 `data` 字段的内容。"""
    try:
        # trust_env=False：忽略 HTTP_PROXY / ALL_PROXY 等环境变量，
        # 后端只接受本机回环直连，走代理会因转发头被 403 拒绝
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), trust_env=False
        ) as client:
            resp = await client.request(method, _url(path))
    except httpx.HTTPError as e:
        raise APIError(0, f"无法连接后端：{e.__class__.__name__}") from e

    if resp.status_code >= 400:
        try:
            payload = resp.json()
            msg = payload.get("details") or payload.get("detail") or resp.text
        except ValueError:
            msg = resp.text
        hint = _STATUS_HINTS.get(resp.status_code)
        if hint:
            msg = hint if not msg or msg == hint else f"{hint}\n{msg}"
        raise APIError(resp.status_code, msg)

    try:
        payload = resp.json()
    except ValueError as e:
        raise APIError(resp.status_code, "后端响应不是合法 JSON") from e
    return payload.get("data", payload)


# ─── 公开接口 ────────────────────────────────────────────────


async def ping() -> dict:
    """确认后端可达。"""
    return await _request("GET", "/ping")


async def get_server_status() -> dict:
    """服务器状态聚合。

    - 没有活跃实例：`{"available": false}`
    - 有活跃实例：`available`、`online`、`ip`、`players`、`uptimeSeconds`、
      `release`、`memory`、`cpu`；后三项可能为 null。
    """
    return await _request("GET", "/server-status")


async def trigger_one_click_start() -> dict:
    """触发 oneclick_start_game，返回 `taskId` / `status` / `alreadyRunning`。"""
    return await _request("POST", "/trigger-one-click-start")