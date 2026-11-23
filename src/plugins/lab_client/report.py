"""服务器状态 / 一键启动的共享实现。

`/状态`、`/启动` 指令与 AI 的 `get_server_status` / `start_server` 工具
都调用这里，保证群里看到的文案和 agent 拿到的工具结果完全一致。
函数本身不抛异常，失败也返回可直接展示的文本。
"""

from __future__ import annotations

from datetime import datetime

from nonebot.log import logger

from . import api


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


def format_status(data: dict) -> str:
    """把 `/server-status` 的响应格式化成一段人类可读文本。"""
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


async def server_status_text() -> str:
    """查询服务器状态；失败时返回带原因的错误文本。"""
    try:
        data = await api.get_server_status()
    except api.APIError as e:
        logger.warning(f"查询服务器状态失败: {e}")
        return f"❌ 查询服务器状态失败：{e.message}"
    return format_status(data if isinstance(data, dict) else {})


async def start_server_text() -> str:
    """触发一键启动；失败时返回带原因的错误文本。"""
    try:
        data = await api.trigger_one_click_start()
    except api.APIError as e:
        logger.warning(f"触发一键启动失败: {e}")
        return f"❌ 启动失败：{e.message}"

    if not isinstance(data, dict):
        data = {}
    task_id = data.get("taskId", "?")
    if data.get("alreadyRunning"):
        return (
            f"⏳ 一键启动任务 #{task_id} 正在执行中，无需重复触发\n"
            "稍后用 /状态 查看进度"
        )
    return (
        f"✅ 已触发一键启动（任务 #{task_id}）\n"
        "稍后用 /状态 查看进度，服务器上线后即可进入"
    )