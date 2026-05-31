"""封装对 go-aliyunmc-v2 后端 API 的 HTTP 调用。"""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import config


class APIError(Exception):
    """后端返回了非 2xx 状态码或业务错误。"""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


def _headers(jwt: str | None = None) -> dict[str, str]:
    h = {
        "X-Bot-Key": config.BOT_KEY,
        "Content-Type": "application/json",
    }
    if jwt:
        h["Authorization"] = f"Bearer {jwt}"
    return h


def _base_url(path: str) -> str:
    return f"{config.BACKEND_URL}{path}"


async def _request(
    method: str,
    path: str,
    jwt: str | None = None,
    body: dict | None = None,
) -> dict[str, Any]:
    """发起 HTTP 请求并处理通用错误格式。"""
    url = _base_url(path)
    headers = _headers(jwt)
    content = json.dumps(body).encode() if body else None

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
        resp = await client.request(method, url, headers=headers, content=content)

    if resp.status_code == 401:
        raise APIError(401, "令牌过期或无效，请重新 /登录")
    if resp.status_code == 403:
        raise APIError(403, "权限不足（可能需要先绑定白名单）")
    if resp.status_code == 404:
        raise APIError(404, "资源不存在")
    if resp.status_code >= 400:
        # 尝试解析后端统一错误格式
        try:
            body_data = resp.json()
            msg = body_data.get("details") or body_data.get("error") or body_data.get("message") or resp.text
        except Exception:
            msg = resp.text
        raise APIError(resp.status_code, msg)

    return resp.json()


# ─── 公开接口 ────────────────────────────────────────────────


async def get_bot_token(username: str, password: str) -> dict:
    """用用户名密码换取 bot JWT（需 X-Bot-Key，无需 session）。"""
    data = await _request(
        "POST",
        "/user/bot-token",
        body={"username": username, "password": password},
    )
    # 后端返回格式：{"data": {"token": ..., "username": ..., ...}}
    # 但 h.B 包装：成功时 data 在顶层，直接返回 body 即可
    return data.get("data", data)


async def get_profile(jwt: str) -> dict:
    """获取当前用户信息。"""
    return (await _request("GET", "/user/profile", jwt=jwt)).get("data", {})


async def get_instance_active(jwt: str) -> dict | None:
    """获取活跃实例，没有则返回 None。"""
    try:
        return (await _request("GET", "/instance/active", jwt=jwt)).get("data", {})
    except APIError as e:
        if e.status == 404:
            return None
        raise


async def get_server_status(jwt: str) -> dict:
    """获取服务器状态快照。"""
    return (await _request("GET", "/state/snapshot/server-status", jwt=jwt)).get(
        "data", {}
    )


async def get_instance_status(jwt: str) -> dict:
    """获取实例状态快照。"""
    return (await _request("GET", "/state/snapshot/instance-status", jwt=jwt)).get(
        "data", {}
    )


async def trigger_start_server(jwt: str) -> dict:
    """触发服务器开机任务。"""
    return (
        await _request(
            "POST",
            "/task/trigger",
            jwt=jwt,
            body={"type": "start_server"},
        )
    ).get("data", {})


async def get_idle_remaining(jwt: str) -> int:
    """获取自动归档剩余秒数。-1 表示没有正在进行的倒计时。"""
    return (
        await _request(
            "GET",
            "/monitor/auto-archive-idle/remaining-secs",
            jwt=jwt,
        )
    ).get("data", -1)


async def sse_watch(path: str, jwt: str):
    """连接 SSE 端点，持续 yield {"event": ..., "data": ...} 字典。"""
    url = _base_url(path)
    headers = _headers(jwt)
    headers["Accept"] = "text/event-stream"

    async with httpx.AsyncClient(timeout=httpx.Timeout(None)) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise APIError(resp.status_code, text.decode()[:200])

            current_event: str | None = None
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    current_event = None
                    continue
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data_str = line[5:].strip()
                    if data_str:
                        yield {
                            "event": current_event,
                            "data": json.loads(data_str),
                        }
