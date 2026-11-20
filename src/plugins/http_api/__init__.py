"""对外 HTTP 接口：其他服务调用后，让 bot 在固定群内发消息。"""

from __future__ import annotations

import ipaddress

from fastapi import HTTPException, Request
from nonebot import get_bot, get_driver
from nonebot.log import logger
from pydantic import BaseModel

from ..lab_client import get_group_openid
from . import config


class SendMessageBody(BaseModel):
    message: str


_FORWARDED_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Real-IP")


def _require_loopback(request: Request) -> None:
    """只接受直接来自本机回环地址的请求。"""
    # 带转发头说明请求经过代理，即使代理在本机也拒绝
    if any(request.headers.get(name) for name in _FORWARDED_HEADERS):
        raise HTTPException(status_code=403, detail="仅允许本机访问")

    host = request.client.host if request.client else ""
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise HTTPException(status_code=403, detail="仅允许本机访问")


async def send_group_message(body: SendMessageBody, request: Request):
    """发送文本消息到 GROUP_OPENID 对应的群。"""
    _require_loopback(request)

    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=400, detail="message 不能为空")
    if len(text) > config.HTTP_API_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"message 超长，最多 {config.HTTP_API_MAX_CHARS} 字符",
        )

    group_openid = get_group_openid()
    if not group_openid:
        raise HTTPException(
            status_code=503,
            detail="群 openid 未知：请配置 GROUP_OPENID，或先在群里 @bot 发一条消息",
        )

    try:
        bot = get_bot()
    except ValueError:
        raise HTTPException(status_code=503, detail="QQ 机器人尚未连接") from None

    if not bot.ready:
        raise HTTPException(status_code=503, detail="QQ 机器人尚未就绪")

    try:
        result = await bot.send_to_group(group_openid=group_openid, message=text)
    except Exception as e:
        logger.error(f"HTTP 接口发群消息失败: {e!r}")
        raise HTTPException(status_code=502, detail=f"QQ 发送失败：{e}") from e

    message_id = getattr(result, "id", None)
    logger.info(f"HTTP 接口已发送群消息 message_id={message_id}")
    return {"ok": True, "group_openid": group_openid, "message_id": message_id}


async def health(request: Request):
    """连通性检查。"""
    _require_loopback(request)
    try:
        bot = get_bot()
        ready = bool(bot.ready)
    except ValueError:
        ready = False
    return {
        "ok": True,
        "bot_ready": ready,
        "group_configured": bool(get_group_openid()),
    }


_driver = get_driver()
_app = getattr(_driver, "server_app", None)
if _app is not None:
    _app.add_api_route("/bot/group-message", send_group_message, methods=["POST"])
    _app.add_api_route("/bot/health", health, methods=["GET"])
    logger.info("HTTP 接口已注册: POST /bot/group-message, GET /bot/health")
else:
    logger.warning("当前 NoneBot 驱动没有 HTTP server，外部接口未启用")
