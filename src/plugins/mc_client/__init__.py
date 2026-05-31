"""QQ 机器人命令和 SSE 状态推送。"""

from __future__ import annotations

import asyncio
import json
import time

from nonebot import get_bot, on_command
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.permission import GROUP, PRIVATE
from nonebot.log import logger

from . import api
from . import config
from . import db

# ─── 启动初始化 ──────────────────────────────────────────────

_BOT_JWT: str = ""
_BOT_JWT_EXP: int = 0
_BOT_LOCK = asyncio.Lock()


async def _ensure_bot_jwt() -> str:
    """获取或刷新 bot 自身的 JWT（用于 SSE 状态监听）。"""
    global _BOT_JWT, _BOT_JWT_EXP

    now = int(time.time())
    if _BOT_JWT and now < _BOT_JWT_EXP - 86400:  # 提前 1 天续期
        return _BOT_JWT

    async with _BOT_LOCK:
        # 双重检查
        if _BOT_JWT and now < _BOT_JWT_EXP - 86400:
            return _BOT_JWT

        resp = await api.get_bot_token(config.BOT_USERNAME, config.BOT_PASSWORD)
        _BOT_JWT = resp["token"]
        _BOT_JWT_EXP = now + 7776000  # 3 个月
        logger.info("Bot 自身 JWT 已刷新")
        return _BOT_JWT


async def _send_group(msg: str) -> None:
    """向目标群发送消息。"""
    try:
        bot = get_bot()
        await bot.send_group_msg(
            group_id=int(config.GROUP_ID),
            message=Message(msg),
        )
    except Exception:
        logger.warning(f"发送群消息失败，可能是 bot 未连接或群号错误")


# ─── SSE 状态监听 ─────────────────────────────────────────────

async def _watch_sse(path: str, formatter):
    """连接 SSE 端点，状态变更时推送到群。"""
    while True:
        try:
            jwt = await _ensure_bot_jwt()
            logger.info(f"连接 SSE: {path}")
            async for event in api.sse_watch(path, jwt):
                msg = formatter(event)
                if msg:
                    await _send_group(msg)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"SSE {path} 异常，5 秒后重连: {e}")
            await asyncio.sleep(5)


def _fmt_server_status(event: dict) -> str | None:
    """格式化服务器状态变更消息。第一次收到 snapshot 不推送。"""
    event_type = event.get("event")
    data = event.get("data", {})
    if event_type == "server_status_snapshot":
        return None  # 快照不推送，只等变更
    if event_type == "server_status_update":
        value = data.get("Value", {})
        online = value.get("online")
        players = value.get("playerCount", 0)
        if online:
            return f"🟢 服务器已上线！当前 {players} 人在线"
        else:
            return f"🔴 服务器已离线"
    return None


def _fmt_instance_status(event: dict) -> str | None:
    """格式化实例状态变更消息。"""
    event_type = event.get("event")
    data = event.get("data", {})
    if event_type == "instance_status_update":
        value = data.get("Value", "")
        if value:
            return f"📦 实例状态: {value}"
    return None


# ─── 命令：/登录 ──────────────────────────────────────────────

login_cmd = on_command("登录", permission=PRIVATE, priority=5)


@login_cmd.handle()
async def handle_login(event: PrivateMessageEvent):
    """私聊 /登录 用户名 密码"""
    args = event.get_plaintext().strip().split()
    if len(args) < 3:
        await login_cmd.finish("用法：/登录 用户名 密码")
        return

    username = args[1]
    password = args[2]

    try:
        resp = await api.get_bot_token(username, password)
    except api.APIError as e:
        await login_cmd.finish(f"登录失败：{e.message}")
        return

    token = resp.get("token")
    wl = resp.get("whitelist_uuid")
    exp_ts = int(time.time()) + 7776000  # 3 个月

    qq = str(event.user_id)
    await db.save_token(qq, username, token, exp_ts, wl)

    wl_info = f"，已绑定玩家" if wl else ""
    await login_cmd.finish(
        f"✅ 登录成功！用户 {username}{wl_info}\n"
        f"凭据有效期 3 个月，过期后请重新 /登录"
    )


# ─── 辅助：获取群聊用户的 JWT ──────────────────────────────────

async def _get_user_jwt(event: GroupMessageEvent) -> tuple[str, dict]:
    """从数据库获取发消息用户的 JWT。未登录则抛出友好提示。"""
    qq = str(event.user_id)
    row = await db.get_token(qq)
    if row is None:
        await _send_group("⚠️ 你还没有登录，请先在私聊中使用 /登录 用户名 密码")
        raise RuntimeError("not_logged_in")
    return row["jwt_token"], dict(row)


# ─── 命令：/状态 ──────────────────────────────────────────────

status_cmd = on_command("状态", permission=GROUP, priority=5)


@status_cmd.handle()
async def handle_status(event: GroupMessageEvent):
    try:
        jwt, _ = await _get_user_jwt(event)
    except RuntimeError:
        return

    parts = []

    # 实例信息
    try:
        instance = await api.get_instance_active(jwt)
        if instance:
            parts.append(
                f"📦 活跃实例: {instance.get('instanceId', '?')}\n"
                f"   IP: {instance.get('ip', '未知')}"
            )
        else:
            parts.append("📦 活跃实例: 无")
    except api.APIError as e:
        parts.append(f"📦 实例查询失败: {e.message}")

    # 服务器状态
    try:
        ss = await api.get_server_status(jwt)
        value = ss.get("Value", {})
        if ss.get("Error"):
            parts.append(f"🖥️ 服务器: 查询出错 ({ss['Error']})")
        elif value.get("online"):
            parts.append(f"🖥️ 服务器: 在线，{value.get('playerCount', 0)} 人")
        else:
            parts.append("🖥️ 服务器: 离线")
    except api.APIError as e:
        parts.append(f"🖥️ 服务器查询失败: {e.message}")

    await status_cmd.finish("\n".join(parts))


# ─── 命令：/开机 ──────────────────────────────────────────────

start_cmd = on_command("开机", permission=GROUP, priority=5)


@start_cmd.handle()
async def handle_start(event: GroupMessageEvent):
    try:
        jwt, row = await _get_user_jwt(event)
    except RuntimeError:
        return

    if not row.get("whitelist_uuid"):
        await start_cmd.finish(
            "⚠️ 你尚未绑定白名单（Minecraft 玩家），无法触发开机。\n"
            "请先在网页端绑定后再试。"
        )
        return

    try:
        await api.trigger_start_server(jwt)
        await start_cmd.finish("✅ 已触发服务器开机任务，请稍候查看 /状态")
    except api.APIError as e:
        await start_cmd.finish(f"开机失败：{e.message}")


# ─── 命令：/释放时间 ──────────────────────────────────────────

idle_cmd = on_command("释放时间", aliases={"剩余时间"}, permission=GROUP, priority=5)


@idle_cmd.handle()
async def handle_idle(event: GroupMessageEvent):
    try:
        jwt, _ = await _get_user_jwt(event)
    except RuntimeError:
        return

    try:
        secs = await api.get_idle_remaining(jwt)
        if secs < 0:
            await idle_cmd.finish("⏳ 当前没有进行中的自动归档倒计时")
        else:
            m = secs // 60
            s = secs % 60
            await idle_cmd.finish(f"⏳ 距自动释放剩余: {m} 分 {s} 秒")
    except api.APIError as e:
        await idle_cmd.finish(f"查询失败：{e.message}")


# ─── 命令：/我的信息 ──────────────────────────────────────────

info_cmd = on_command("我的信息", permission=GROUP, priority=5)


@info_cmd.handle()
async def handle_info(event: GroupMessageEvent):
    try:
        jwt, row = await _get_user_jwt(event)
    except RuntimeError:
        return

    parts = [f"👤 用户名: {row['username']}"]

    try:
        profile = await api.get_profile(jwt)
        role = profile.get("role", "?")
        wl = profile.get("whitelist_uuid")
        parts.append(f"🔑 角色: {role}")
        if wl:
            parts.append(f"🎮 已绑定玩家 (UUID): {wl}")
        else:
            parts.append("🎮 未绑定玩家（请先在网页端绑定）")
    except api.APIError:
        parts.append("⚠️ 获取详细信息失败")

    await info_cmd.finish("\n".join(parts))


# ─── 启动事件 ─────────────────────────────────────────────────

from nonebot import get_driver

driver = get_driver()


@driver.on_startup
async def _on_startup():
    await db.init()
    logger.info("数据库初始化完成")

    if not config.BOT_KEY or not config.BOT_USERNAME or not config.GROUP_ID:
        logger.warning("BOT_KEY / BOT_USERNAME / GROUP_ID 未配置，部分功能不可用")
        return

    # 验证 bot 自身账号并获取 JWT
    try:
        await _ensure_bot_jwt()
        logger.info("Bot 自身账号验证成功")
    except Exception as e:
        logger.error(f"Bot 自身账号验证失败: {e}")
        return

    # 启动 SSE 状态监听（后台任务）
    asyncio.create_task(_watch_sse("/state/watch/server-status", _fmt_server_status))
    asyncio.create_task(
        _watch_sse("/state/watch/instance-status", _fmt_instance_status)
    )
    logger.info("SSE 状态监听已启动")


@driver.on_shutdown
async def _on_shutdown():
    await db.close()
