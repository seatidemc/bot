"""AI 对话插件：非指令消息 → router → chat / knowledge agent。

整体结构：
- / 开头的消息一律视为指令；未知指令由 lab_client 的兜底匹配器回复。
- 非指令消息进入本插件；AI 关闭时回复「暂未启动AI」。
- AI 开启时先由 router 判断是普通聊天还是知识库问答，再交给对应 agent。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from nonebot import get_driver, on_message
from nonebot.adapters.qq import (
    Bot,
    C2CMessageCreateEvent,
    GroupMessageCreateEvent,
)
from nonebot.log import logger
from nonebot.permission import Permission
from nonebot.rule import Rule

from ..lab_client import is_command_text, is_group_addressed
from . import api, config, memory, tools, workflow

# ─── 权限：私聊 / 群聊 ────────────────────────────────────────

PRIVATE = Permission(lambda event: isinstance(event, C2CMessageCreateEvent))
GROUP_ANY = Permission(lambda event: isinstance(event, GroupMessageCreateEvent))
PRIVATE_OR_ANY_GROUP = PRIVATE | GROUP_ANY


def _chat_allowed(event) -> bool:
    """决定哪些消息会进入 AI 工作流。"""
    if isinstance(event, C2CMessageCreateEvent):
        return not is_command_text(event.get_plaintext().strip())

    if not isinstance(event, GroupMessageCreateEvent):
        return False
    if getattr(getattr(event, "author", None), "bot", False):
        return False

    text = event.get_plaintext().strip()
    if is_command_text(text):
        # 指令（包括未知指令）交给 lab_client 处理
        return False
    if is_group_addressed(event):
        return True
    # 未 @ 的群消息只有在 AI 开启且显式允许自动聊天时才处理
    return config.AI_ENABLED and config.AI_GROUP_CHAT_WITHOUT_AT


chat_allowed = Rule(_chat_allowed)

_user_locks: dict[str, asyncio.Lock] = {}
_semaphore = asyncio.Semaphore(max(1, config.AI_MAX_CONCURRENCY))


def _history_key(event) -> str:
    if isinstance(event, GroupMessageCreateEvent):
        return f"group:{event.group_openid}:{event.get_user_id()}"
    return f"c2c:{event.get_user_id()}"


def _lock_for(key: str) -> asyncio.Lock:
    lock = _user_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


_auto_chat_times: dict[str, deque[float]] = {}
_auto_chat_lock = asyncio.Lock()


async def _allow_auto_chat(group_openid: str) -> bool:
    """未 @ 的群聊按群限流；返回是否允许本次自动回复。"""
    limit = config.AI_GROUP_CHAT_PER_MINUTE
    if limit <= 0:
        return False
    now = time.monotonic()
    async with _auto_chat_lock:
        times = _auto_chat_times.setdefault(group_openid, deque())
        while times and now - times[0] >= 60:
            times.popleft()
        if len(times) >= limit:
            return False
        times.append(now)
        return True


# ─── 聊天匹配器 ──────────────────────────────────────────────

chat = on_message(
    rule=chat_allowed, permission=PRIVATE_OR_ANY_GROUP, priority=10, block=True
)


@chat.handle()
async def handle_chat(bot: Bot, event):
    text = event.get_plaintext().strip()
    is_group = isinstance(event, GroupMessageCreateEvent)
    addressed = is_group_addressed(event) if is_group else True

    if not text:
        if addressed or not is_group:
            await chat.finish("目前只支持文字聊天哦，直接发文字给我就行～")
        return

    if not config.AI_ENABLED:
        await chat.finish("暂未启动AI")
        return

    if is_group and not addressed:
        if not await _allow_auto_chat(event.group_openid):
            logger.debug(
                f"自动聊天达到每分钟上限 {config.AI_GROUP_CHAT_PER_MINUTE}，跳过"
            )
            return

    key = _history_key(event)

    async with _lock_for(key):
        # 同一个人连发多条时排队，避免上下文交错
        history = memory.get_messages(key)

        async with _semaphore:
            try:
                result = await workflow.run(text, history)
            except api.LLMError as e:
                logger.warning(f"AI 调用失败: {e}")
                await chat.finish(f"😥 AI 暂时不可用：{e.message}")
                return
            except Exception:
                logger.exception("AI 工作流执行异常")
                await chat.finish("😥 AI 暂时不可用，请稍后再试")
                return

    reply = result.reply
    if not reply:
        reply = "（AI 没有返回内容，换个问法试试？）"

    memory.add_turn(key, text, reply)
    await chat.finish(reply)


# ─── 启动日志 ────────────────────────────────────────────────

@get_driver().on_startup
async def _on_startup():
    if not config.AI_ENABLED:
        logger.warning(
            "AI 已关闭（AI_ENABLED=false），"
            "非指令消息将回复「暂未启动AI」"
        )
        return

    problems = config.validate_agent_configs()
    if problems:
        for problem in problems:
            logger.error(f"AI 配置检查失败：{problem}")
        raise RuntimeError(
            "AI 配置检查失败（AI_ENABLED=true）：\n- "
            + "\n- ".join(problems)
        )

    logger.info(
        "AI 工作流已启用："
        f"router={config.ROUTER.model} "
        f"chat={config.CHAT.model} "
        f"knowledge={config.KNOWLEDGE.model}"
    )
    logger.info(f"知识库索引：{config.AI_KNOWLEDGE_LLMS_TXT_URL}")

    try:
        index = await tools.check_knowledge_index()
    except tools.KnowledgeError as e:
        logger.error(f"AI 配置检查失败：llms.txt 不可用：{e}")
        raise RuntimeError(
            f"AI 配置检查失败：llms.txt 不可用：{e}"
        ) from e
    logger.info(f"llms.txt 检查通过：{len(index.entries)} 条")

    if config.AI_GROUP_CHAT_WITHOUT_AT:
        logger.info(
            f"群内自动聊天已开启，每分钟最多 {config.AI_GROUP_CHAT_PER_MINUTE} 条"
        )