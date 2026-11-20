"""QQ 机器人命令（QQ 官方 API / nonebot-adapter-qq）。"""

from __future__ import annotations

import time
from pathlib import Path

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.qq import (
    Bot,
    C2CMessageCreateEvent,
    GroupMessageCreateEvent,
    MessageSegment,
)
from nonebot.log import logger
from nonebot.permission import Permission

from . import api
from . import config
from . import db

# ─── 启动初始化 ──────────────────────────────────────────────

# 自动发现的群 openid：首次收到群消息时记录，并持久化到 data/group_openid.txt
_GROUP_OPENID_FILE = Path("data/group_openid.txt")


def _load_discovered_group_openid() -> str:
    try:
        return _GROUP_OPENID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


_discovered_group_openid: str = _load_discovered_group_openid()


def _get_group_openid() -> str:
    """获取目标群 openid，优先使用配置值，其次自动发现的值。"""
    if config.GROUP_OPENID:
        return config.GROUP_OPENID
    return _discovered_group_openid


def get_group_openid() -> str:
    """对外暴露：当前目标群 openid（配置优先，其次自动发现）。"""
    return _get_group_openid()


# ─── 权限：私聊 vs 群聊 ────────────────────────────────────────


async def _is_private(event) -> bool:
    return isinstance(event, C2CMessageCreateEvent)


def is_group_addressed(event) -> bool:
    """群消息是否在呼叫机器人：@ 它或回复它的消息。"""
    if not isinstance(event, GroupMessageCreateEvent):
        return False
    if getattr(event, "to_me", False):
        return True
    for mention in getattr(event, "mentions", None) or []:
        if getattr(mention, "scope", "single") == "single" and getattr(
            mention, "is_you", False
        ):
            return True
    try:
        for seg in event.get_message():
            if seg.type == "mention_user" and seg.data.get("is_bot"):
                return True
    except Exception:
        pass
    return False


async def _is_group(event) -> bool:
    return is_group_addressed(event)


PRIVATE = Permission(_is_private)
GROUP = Permission(_is_group)
PRIVATE_OR_GROUP = PRIVATE | GROUP


# ─── 自动发现群 openid ────────────────────────────────────────

_group_probe = on_message(priority=1, block=False)


@_group_probe.handle()
async def _probe_group(event: GroupMessageCreateEvent):
    """收到任意群消息时记录群 openid，供 HTTP 接口发消息使用。"""
    global _discovered_group_openid
    logger.debug(
        f"群消息: {type(event).__name__} to_me={getattr(event, 'to_me', False)}"
    )
    openid = getattr(event, "group_openid", "")
    if not openid or openid == _discovered_group_openid:
        return
    _discovered_group_openid = openid
    try:
        _GROUP_OPENID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _GROUP_OPENID_FILE.write_text(openid + "\n", encoding="utf-8")
    except OSError:
        logger.warning("写入 data/group_openid.txt 失败，仅保存在内存中")
    logger.info(f"已记录群 openid: {openid}（也可填入 GROUP_OPENID 固定使用）")


# ─── 命令：/登录 ──────────────────────────────────────────────

login_cmd = on_command("登录", aliases={"login"}, permission=PRIVATE, priority=5)


@login_cmd.handle()
async def handle_login(event: C2CMessageCreateEvent):
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
    exp_ts = int(time.time()) + 7776000

    user_id = event.get_user_id()
    await db.save_token(user_id, username, token, exp_ts, wl)

    wl_info = "，已绑定玩家" if wl else ""
    await login_cmd.finish(
        f"✅ 登录成功！用户 {username}{wl_info}\n"
        "凭据有效期 3 个月，过期后请重新 /登录"
    )


# ─── 辅助：获取群聊用户的 JWT ──────────────────────────────────

async def _get_user_jwt(
    bot: Bot, event: C2CMessageCreateEvent | GroupMessageCreateEvent
) -> tuple[str, dict]:
    """从数据库获取发消息用户的 JWT。未登录则直接回复提醒。"""
    user_id = event.get_user_id()
    row = await db.get_token(user_id)
    if row is None:
        await bot.send(
            event,
            MessageSegment.text("⚠️ 你还没有登录，请先在私聊中使用 /登录 用户名 密码"),
        )
        raise RuntimeError("not_logged_in")
    return row["jwt_token"], dict(row)


# ─── 命令：/状态 ──────────────────────────────────────────────

status_cmd = on_command("状态", aliases={"status", "zt"}, permission=PRIVATE_OR_GROUP, priority=5)


@status_cmd.handle()
async def handle_status(bot: Bot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    try:
        jwt, _ = await _get_user_jwt(bot, event)
    except RuntimeError:
        return

    parts = []

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

start_cmd = on_command("开机", aliases={"start", "kj"}, permission=PRIVATE_OR_GROUP, priority=5)


@start_cmd.handle()
async def handle_start(bot: Bot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    try:
        jwt, row = await _get_user_jwt(bot, event)
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

idle_cmd = on_command(
    "释放时间", aliases={"idle", "sfsj", "剩余时间"}, permission=PRIVATE_OR_GROUP, priority=5
)


@idle_cmd.handle()
async def handle_idle(bot: Bot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    try:
        jwt, _ = await _get_user_jwt(bot, event)
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

info_cmd = on_command(
    "我的信息", aliases={"info", "wdxx", "profile"}, permission=PRIVATE_OR_GROUP, priority=5
)


@info_cmd.handle()
async def handle_info(bot: Bot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    try:
        jwt, row = await _get_user_jwt(bot, event)
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


# ─── 兜底：私聊非命令消息 ─────────────────────────────────────

_fallback = on_message(permission=PRIVATE, priority=99, block=False)


@_fallback.handle()
async def handle_fallback(event: C2CMessageCreateEvent):
    text = event.get_plaintext().strip()
    # 所有已知命令，留给 on_command handler 处理
    _cmds = ("/登录", "/login", "/状态", "/status", "/zt",
             "/开机", "/start", "/kj", "/释放时间", "/idle", "/sfsj",
             "/剩余时间", "/我的信息", "/info", "/wdxx", "/profile",
             "/重置对话", "/reset", "/清空对话", "/clear")
    if any(text.startswith(c) for c in _cmds):
        return
    await _fallback.finish(
        "可用命令：\n"
        "  /登录 用户名 密码\n"
        "  /状态\n"
        "  /开机\n"
        "  /释放时间\n"
        "  /我的信息\n"
        "  /重置对话"
    )


# ─── 启动事件 ─────────────────────────────────────────────────

driver = get_driver()


@driver.on_startup
async def _on_startup():
    await db.init()
    logger.info("数据库初始化完成")


@driver.on_shutdown
async def _on_shutdown():
    await db.close()
