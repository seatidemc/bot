"""群聊聊天记录的永久存储（SQLite）。

记录按 ``group_openid`` 隔离，不区分用户：同一个群里所有人的发言和 bot
的回复构成同一份上下文。消息只增不删，用于：

- 每次请求时预置最近的聊天内容；
- chat agent 通过 ``search_chat_log`` 工具检索更早的记录。

数据库固定放在 ``data/chat_log.db``，随 ``data/`` 一起被 .gitignore 排除。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from nonebot.log import logger

_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "chat_log.db"
_DB_LOCK = asyncio.Lock()
_ready = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    user_openid  TEXT    NOT NULL DEFAULT '',
    username     TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL,
    message_id   TEXT,
    created_at   REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_log_group_id
    ON chat_log(group_openid, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_log_group_message
    ON chat_log(group_openid, message_id) WHERE message_id IS NOT NULL;
"""

T = TypeVar("T")


@dataclass(frozen=True)
class ChatLogEntry:
    role: str
    user_openid: str
    username: str
    content: str
    created_at: float


def db_path() -> Path:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


async def _run(fn: Callable[[sqlite3.Connection], T]) -> T:
    """串行执行一次数据库操作，避免并发写锁冲突。"""

    def work() -> T:
        global _ready
        conn = _connect()
        try:
            if not _ready:
                conn.executescript(_SCHEMA)
                _ready = True
            return fn(conn)
        finally:
            conn.close()

    async with _DB_LOCK:
        return await asyncio.to_thread(work)


async def record_user(
    group_openid: str,
    user_openid: str,
    content: str,
    *,
    message_id: str | None = None,
    username: str = "",
    created_at: float | None = None,
) -> None:
    """记录一条用户群消息；message_id 相同的重复投递会被忽略。"""

    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO chat_log "
            "(group_openid, role, user_openid, username, content, "
            " message_id, created_at) "
            "VALUES (?, 'user', ?, ?, ?, ?, ?)",
            (
                group_openid,
                user_openid,
                username,
                content,
                message_id,
                created_at if created_at is not None else time.time(),
            ),
        )
        conn.commit()

    await _run(op)


async def record_bot(
    group_openid: str,
    content: str,
    *,
    created_at: float | None = None,
) -> None:
    """记录一条 bot 在群里发出的回复。"""

    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO chat_log "
            "(group_openid, role, user_openid, username, content, "
            " message_id, created_at) "
            "VALUES (?, 'bot', '', '', ?, NULL, ?)",
            (
                group_openid,
                content,
                created_at if created_at is not None else time.time(),
            ),
        )
        conn.commit()

    await _run(op)


async def recent_messages(
    group_openid: str,
    limit: int,
    *,
    exclude_message_id: str | None = None,
) -> list[dict[str, str]]:
    """读取群里最近的聊天记录，返回可直接放进 messages 的列表。

    结果按时间正序；``exclude_message_id`` 用于排除当前正在处理的这条
    消息，避免在 agent 里重复出现。
    """
    if limit <= 0:
        return []

    def op(conn: sqlite3.Connection) -> list[dict[str, str]]:
        rows = conn.execute(
            "SELECT role, content FROM chat_log "
            "WHERE group_openid = ? "
            "  AND (? IS NULL OR message_id IS NULL OR message_id != ?) "
            "ORDER BY id DESC LIMIT ?",
            (group_openid, exclude_message_id, exclude_message_id, limit),
        ).fetchall()
        return [
            {
                "role": "assistant" if row["role"] == "bot" else "user",
                "content": row["content"],
            }
            for row in reversed(rows)
        ]

    return await _run(op)


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search(
    group_openid: str,
    keywords: list[str] | tuple[str, ...] = (),
    *,
    since: float | None = None,
    until: float | None = None,
    limit: int = 50,
) -> list[ChatLogEntry]:
    """按时间范围和关键词检索群聊记录。

    多个关键词之间是 AND 关系（每条关键词都要在内容中命中）；结果按
    时间正序返回，最多 ``limit`` 条（取最近的若干条）。
    """
    take = max(1, min(int(limit), 200))
    sql = (
        "SELECT role, user_openid, username, content, created_at "
        "FROM chat_log WHERE group_openid = ?"
    )
    params: list[Any] = [group_openid]
    if since is not None:
        sql += " AND created_at >= ?"
        params.append(float(since))
    if until is not None:
        sql += " AND created_at < ?"
        params.append(float(until))
    for term in keywords:
        sql += " AND content LIKE ? ESCAPE '\\'"
        params.append(f"%{_escape_like(term)}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(take)

    def op(conn: sqlite3.Connection) -> list[ChatLogEntry]:
        rows = conn.execute(sql, params).fetchall()
        return [
            ChatLogEntry(
                role=row["role"],
                user_openid=row["user_openid"],
                username=row["username"],
                content=row["content"],
                created_at=row["created_at"],
            )
            for row in reversed(rows)
        ]

    entries = await _run(op)
    logger.debug(
        f"群聊记录检索：group={group_openid} "
        f"keywords={list(keywords)} 命中 {len(entries)} 条"
    )
    return entries