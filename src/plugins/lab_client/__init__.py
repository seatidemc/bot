"""QQ 机器人命令（QQ 官方 API / nonebot-adapter-qq）。

只对接后端 `/bot-v1` 的本机回环接口，不涉及用户登录态：
    /状态  → GET  /bot-v1/server-status
    /启动 → POST /bot-v1/trigger-one-click-start
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from nonebot import on_command, on_message
from nonebot.adapters.qq import GroupMessageCreateEvent
from nonebot.log import logger
from nonebot.permission import Permission
from nonebot.rule import Rule

from . import api
from . import config

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


# ─── 权限：仅群聊 ────────────────────────────────────────────


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


# 目前只支持群聊：所有指令都要求 @bot
GROUP = Permission(_is_group)


def is_command_text(text: str) -> bool:
    """以 / 或 ／ 开头的消息一律视为指令，无论指令是否存在。"""
    return text.startswith(("/", "／"))


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


# ─── 状态格式化 ──────────────────────────────────────────────


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours} 小时"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分" + (f" {secs} 秒" if secs else "")
    return f"{secs} 秒"


def _fmt_gb(num_bytes: float) -> str:
    return f"{num_bytes / 1024**3:.1f} GB"


def _fmt_percent(value) -> str:
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return "未知"


def _format_status(data: dict) -> str:
    if not data.get("available"):
        return "📦 当前没有活跃的服务器实例\n发送 /启动 可一键创建并启动"

    lines = ["🖥️ 服务器：" + ("在线" if data.get("online") else "离线")]

    ip = data.get("ip") or ""
    if ip:
        lines.append(f"🌐 地址：{ip}")

    players = data.get("players") or {}
    online = players.get("online", 0)
    max_players = players.get("max")
    if max_players:
        lines.append(f"👥 玩家：{online} / {max_players}")
    else:
        lines.append(f"👥 玩家：{online} 人")

    uptime = data.get("uptimeSeconds")
    if uptime is not None:
        lines.append(f"⏱️ 已运行：{_fmt_duration(uptime)}")

    resources = []
    memory = data.get("memory")
    if memory:
        resources.append(
            f"内存 {_fmt_percent(memory.get('usagePercent'))}"
            f"（{_fmt_gb(memory.get('usedBytes', 0))} / "
            f"{_fmt_gb(memory.get('totalBytes', 0))}）"
        )
    cpu = data.get("cpu")
    if cpu:
        resources.append(
            f"CPU {_fmt_percent(cpu.get('usagePercent'))}"
            f"（{cpu.get('usedCores')} / {cpu.get('cores')} 核）"
        )
    if resources:
        lines.append("📊 " + "，".join(resources))

    release = data.get("release")
    if release:
        secs = release.get("inSeconds", 0)
        at = release.get("at")
        when = datetime.fromtimestamp(at).strftime("%m-%d %H:%M") if at else ""
        if secs and secs > 0:
            lines.append(
                f"⏳ 预计释放：{_fmt_duration(secs)}后" + (f"（{when}）" if when else "")
            )
        else:
            lines.append("⏳ 关服收尾中，即将释放")

    return "\n".join(lines)


# ─── 命令：/状态 ──────────────────────────────────────────────

# on_command 默认 block=False（NoneBot 2.5），必须显式 block，
# 否则已知指令处理完后仍会落到下面的未知指令兜底。
status_cmd = on_command(
    "状态", aliases={"status", "zt"}, permission=GROUP, priority=5, block=True
)


@status_cmd.handle()
async def handle_status():
    try:
        data = await api.get_server_status()
    except api.APIError as e:
        await status_cmd.finish(f"❌ 查询服务器状态失败：{e.message}")
        return

    await status_cmd.finish(_format_status(data))


# ─── 命令：/启动 ──────────────────────────────────────────────

start_cmd = on_command(
    "启动", aliases={"start", "qd"}, permission=GROUP, priority=5, block=True
)


@start_cmd.handle()
async def handle_start():
    try:
        data = await api.trigger_one_click_start()
    except api.APIError as e:
        await start_cmd.finish(f"❌ 启动失败：{e.message}")
        return

    task_id = data.get("taskId", "?")
    if data.get("alreadyRunning"):
        await start_cmd.finish(
            f"⏳ 一键启动任务 #{task_id} 正在执行中，无需重复触发\n"
            "稍后用 /状态 查看进度"
        )
    else:
        await start_cmd.finish(
            f"✅ 已触发一键启动（任务 #{task_id}）\n"
            "稍后用 /状态 查看进度，服务器上线后即可进入"
        )


# ─── 兜底：未知指令 ─────────────────────────────────────────

_HELP_TEXT = (
    "可用指令：\n"
    "  /状态  查看服务器状态\n"
    "  /启动  一键启动服务器"
)


def _is_unknown_command(event) -> bool:
    return is_command_text(event.get_plaintext().strip())


# 已知指令由上面的 on_command（显式 block=True）以更高优先级处理并阻断传播；
# 能走到这里的斜杠开头消息就是未知指令。
_unknown_command = on_message(
    rule=Rule(_is_unknown_command),
    permission=GROUP,
    priority=50,
    block=True,
)


@_unknown_command.handle()
async def handle_unknown_command(event):
    text = event.get_plaintext().strip()
    name = text.split(maxsplit=1)[0] if text else "/"
    await _unknown_command.finish(f"❓ 指令不存在：{name}\n{_HELP_TEXT}")