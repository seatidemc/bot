"""AI 对话插件：群内非指令消息 → router → chat / knowledge agent。

整体结构：
- / 开头的消息一律视为指令；未知指令由 lab_client 的兜底匹配器回复。
- 非指令群消息进入本插件；AI 关闭时回复「暂未启动AI」。
- AI 开启时先由 router 判断是普通聊天还是知识库问答，再交给对应 agent。
- 只处理群聊；私聊消息直接忽略。
- 群聊记录永久写入 data/chat_log.db，群内所有人共享同一份上下文。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

from nonebot import get_driver, on_message
from nonebot.adapters.qq import Bot, GroupMessageCreateEvent
from nonebot.log import logger
from nonebot.permission import Permission
from nonebot.rule import Rule

from ..lab_client import is_command_text, is_group_addressed
from . import api, chat_log, config, tools, workflow

# ─── 匹配器 ──────────────────────────────────────────────────

GROUP_ANY = Permission(lambda event: isinstance(event, GroupMessageCreateEvent))


def _chat_allowed(event) -> bool:
    """决定哪些群消息会进入 AI 工作流。"""
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

# 全局互斥：同一时间只允许一条 AI 链路在跑
_chain_lock = asyncio.Lock()

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


# ─── 群聊记录 ────────────────────────────────────────────────

# 所有群消息（含指令、未 @ 的闲聊）都先落库，供上下文和工具检索
recorder = on_message(priority=1, block=False)


@recorder.handle()
async def _record_group_message(event):
    if not isinstance(event, GroupMessageCreateEvent):
        return
    author = getattr(event, "author", None)
    if getattr(author, "bot", False):
        return

    text = event.get_plaintext().strip()
    if not text:
        return

    try:
        await chat_log.record_user(
            event.group_openid,
            event.get_user_id(),
            text,
            message_id=getattr(event, "id", None) or None,
            username=getattr(author, "username", "") or "",
        )
    except Exception:
        logger.exception("群聊记录写入失败")


async def _load_history(event: GroupMessageCreateEvent) -> list[dict[str, str]]:
    """群聊上下文直接取自永久聊天记录，排除正在处理的这条消息。

    只取最近 ``AI_CHAT_LOG_RECENT`` 条，且不早于
    ``AI_CHAT_LOG_WINDOW_MINUTES`` 分钟前。
    """
    limit = config.AI_CHAT_LOG_RECENT
    window = config.AI_CHAT_LOG_WINDOW_MINUTES
    since = time.time() - window * 60 if window > 0 else None
    try:
        history = await chat_log.recent_messages(
            event.group_openid,
            limit,
            exclude_message_id=getattr(event, "id", None) or None,
            since=since,
        )
    except Exception:
        logger.exception("群聊记录读取失败，本次使用空上下文")
        return []

    span = f"{window} 分钟内" if window > 0 else "不限时间"
    logger.info(
        f"群聊上下文：预置 {len(history)} 条（上限 {limit} 条，{span}）"
    )
    return history


# ─── 聊天匹配器 ──────────────────────────────────────────────


def _user_facing_error(error: api.LLMError) -> str:
    """把模型错误映射成群内提示，不暴露端点、状态码和原始报错。"""
    if error.status == 429:
        return "😥 请求太频繁了，请稍后再试"
    return "😥 AI 暂时不可用，请稍后再试"


chat = on_message(
    rule=chat_allowed, permission=GROUP_ANY, priority=10, block=True
)


@chat.handle()
async def handle_chat(bot: Bot, event: GroupMessageCreateEvent):
    text = event.get_plaintext().strip()
    addressed = is_group_addressed(event)

    if not text:
        if addressed:
            await chat.finish("目前只支持文字聊天哦，直接发文字给我就行～")
        return

    if not config.AI_ENABLED:
        await chat.finish("暂未启动AI")
        return

    if _chain_lock.locked():
        if addressed:
            await chat.finish("上一条消息正在处理中，请稍后再问")
        logger.debug("AI 链路正在处理其他消息，跳过未 @ 的群消息")
        return

    if not addressed:
        if not await _allow_auto_chat(event.group_openid):
            logger.debug(
                f"自动聊天达到每分钟上限 {config.AI_GROUP_CHAT_PER_MINUTE}，跳过"
            )
            return
        # 限流检查期间可能已有链路启动，再确认一次
        if _chain_lock.locked():
            logger.debug("AI 链路正在处理其他消息，跳过未 @ 的群消息")
            return

    # 检查与获取之间没有 await，保证互斥判断和拿锁是原子的
    async with _chain_lock:
        # 工具需要知道当前群；私聊不进入本插件，所以一定有值
        token = tools.set_group_context(event.group_openid)
        try:
            history = await _load_history(event)
            try:
                result = await workflow.run(text, history)
            except api.LLMError as e:
                # 具体错误只写日志，群里只给稳定、不含内部信息的提示
                logger.warning(f"AI 调用失败: {e}")
                await chat.finish(_user_facing_error(e))
            except Exception:
                logger.exception("AI 工作流执行异常")
                await chat.finish("😥 AI 暂时不可用，请稍后再试")
        finally:
            tools.reset_group_context(token)

    reply = result.reply
    if not reply:
        reply = "（AI 没有返回内容，换个问法试试？）"

    try:
        await chat_log.record_bot(event.group_openid, reply)
    except Exception:
        logger.exception("群聊记录写入失败")
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
        f"router={config.ROUTER.model}（System One，"
        f"{config.ROUTER.base_url}） "
        f"chat={config.CHAT.model} "
        f"knowledge={config.KNOWLEDGE.model}"
    )
    logger.info(
        f"群聊记录：{chat_log.db_path()}，"
        f"上下文预置上限 {config.AI_CHAT_LOG_RECENT} 条"
        + (
            f"（{config.AI_CHAT_LOG_WINDOW_MINUTES} 分钟内）"
            if config.AI_CHAT_LOG_WINDOW_MINUTES > 0
            else "（不限时间）"
        )
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