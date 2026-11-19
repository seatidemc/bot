"""DeepSeek 智能对话：私聊任意消息、群内 @bot 即可聊天。"""

from __future__ import annotations

import asyncio

from nonebot import get_driver, on_command, on_message
from nonebot.adapters.qq import (
    Bot,
    C2CMessageCreateEvent,
    GroupAtMessageCreateEvent,
)
from nonebot.log import logger
from nonebot.permission import Permission
from nonebot.rule import Rule

from . import api, config, memory

# ─── 权限：私聊 / 群聊 ────────────────────────────────────────

PRIVATE = Permission(lambda event: isinstance(event, C2CMessageCreateEvent))
GROUP = Permission(lambda event: isinstance(event, GroupAtMessageCreateEvent))
PRIVATE_OR_GROUP = PRIVATE | GROUP

# 没有 API Key 时整个聊天匹配器不生效，退回 mc_client 的命令提示
ai_enabled = Rule(lambda event: config.AI_ENABLED)

_user_locks: dict[str, asyncio.Lock] = {}
_semaphore = asyncio.Semaphore(max(1, config.AI_MAX_CONCURRENCY))


def _history_key(event) -> str:
    if isinstance(event, GroupAtMessageCreateEvent):
        return f"group:{event.group_openid}:{event.get_user_id()}"
    return f"c2c:{event.get_user_id()}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _user_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


HELP_TEXT = (
    "可用命令：\n"
    "  /登录 用户名 密码（仅私聊）\n"
    "  /状态\n"
    "  /开机\n"
    "  /释放时间\n"
    "  /我的信息\n"
    "  /重置对话\n"
    "其他问题直接发文字和我聊就行～"
)


# ─── 聊天匹配器 ──────────────────────────────────────────────

chat = on_message(
    rule=ai_enabled, permission=PRIVATE_OR_GROUP, priority=10, block=True
)


@chat.handle()
async def handle_chat(bot: Bot, event):
    text = event.get_plaintext().strip()
    if not text:
        await chat.finish("目前只支持文字聊天哦，直接发文字给我就行～")

    # 命令交给 mc_client 处理；这里兜住群里权限不符或拼错的命令，避免内容被发到 AI
    if text.startswith(("/", "／")):
        await chat.finish(HELP_TEXT)

    if len(text) > config.AI_MAX_INPUT_CHARS:
        text = text[: config.AI_MAX_INPUT_CHARS]

    key = _history_key(event)

    async with _lock_for(key):
        # 同一个人连发多条时排队，避免上下文交错
        history = memory.get_messages(key)
        messages = [{"role": "system", "content": config.AI_SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": text})

        async with _semaphore:
            try:
                reply = await api.chat(messages)
            except api.DeepSeekError as e:
                logger.warning(f"DeepSeek 调用失败: {e}")
                await chat.finish(f"😥 AI 暂时不可用：{e.message}")
                return

    if not reply:
        reply = "（AI 没有返回内容，换个问法试试？）"
    if len(reply) > config.AI_MAX_REPLY_CHARS:
        reply = reply[: config.AI_MAX_REPLY_CHARS].rstrip() + "…"

    memory.add_turn(key, text, reply)
    await chat.finish(reply)


# ─── 命令：/重置对话 ─────────────────────────────────────────

reset_cmd = on_command(
    "重置对话",
    aliases={"reset", "清空对话", "clear"},
    permission=PRIVATE_OR_GROUP,
    priority=5,
)


@reset_cmd.handle()
async def handle_reset(event):
    memory.reset(_history_key(event))
    await reset_cmd.finish("🧹 对话上下文已清空，我们重新开始吧")


# ─── 启动日志 ────────────────────────────────────────────────

@get_driver().on_startup
async def _on_startup():
    if config.AI_ENABLED:
        logger.info(f"DeepSeek 对话已启用（模型 {config.DEEPSEEK_MODEL}）")
    else:
        logger.warning("DEEPSEEK_API_KEY 未配置，AI 对话功能已关闭")
