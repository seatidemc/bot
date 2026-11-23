"""群聊聊天记录的永久存储（SQLite）。

记录按 ``group_openid`` 隔离，同一个群里所有人的发言和 bot 的回复构成
同一份上下文。每条用户消息额外保存 ``username`` / ``user_openid`` 和
时间：作为 user 消息预置时，按「用户名#标识 | 时间 | 发言内容」区分
发言人，标识取 openid 后 6 位。消息只增不删，用于：

- 每次请求时把最近的聊天内容作为「最近聊天记录」user 消息注入
  （前面还有一条预置的「当前时间」消息）；
- chat agent 通过 ``recall_chat`` 工具检索更早的记录。

数据库固定放在 ``data/chat_log.db``，随 ``data/`` 一起被 .gitignore 排除。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from nonebot.log import logger

from .media import MediaItem, items_from_json, items_to_json

_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "chat_log.db"
_DB_LOCK = asyncio.Lock()
_ready = False

# 显示名里附带的 openid 稳定标识长度
_STABLE_ID_LEN = 6

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_openid TEXT    NOT NULL,
    role         TEXT    NOT NULL,
    user_openid  TEXT    NOT NULL DEFAULT '',
    username     TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL,
    media        TEXT    NOT NULL DEFAULT '[]',
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
    # 这条消息附带的图片 / 视频（本地文件，见 media.py）
    media: tuple[MediaItem, ...] = ()


def db_path() -> Path:
    return _DB_PATH


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _prepare(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # 旧库补 media 列：图片 / 视频记在新增列里，不改动已有数据
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(chat_log)")
    }
    if "media" not in columns:
        conn.execute(
            "ALTER TABLE chat_log "
            "ADD COLUMN media TEXT NOT NULL DEFAULT '[]'"
        )
    conn.commit()


async def _run(fn: Callable[[sqlite3.Connection], T]) -> T:
    """串行执行一次数据库操作，避免并发写锁冲突。"""

    def work() -> T:
        global _ready
        conn = _connect()
        try:
            if not _ready:
                _prepare(conn)
                _ready = True
            return fn(conn)
        finally:
            conn.close()

    async with _DB_LOCK:
        return await asyncio.to_thread(work)


def _entry_from_row(row: sqlite3.Row) -> ChatLogEntry:
    keys = row.keys()
    media_raw = row["media"] if "media" in keys else None
    return ChatLogEntry(
        role=row["role"],
        user_openid=row["user_openid"],
        username=row["username"],
        content=row["content"],
        created_at=row["created_at"],
        media=items_from_json(media_raw),
    )


async def record_user(
    group_openid: str,
    user_openid: str,
    content: str,
    *,
    message_id: str | None = None,
    username: str = "",
    created_at: float | None = None,
    media: Sequence[MediaItem] = (),
) -> None:
    """记录一条用户群消息；message_id 相同的重复投递会被忽略。

    ``media`` 是已经下载到本地的图片 / 视频，按 JSON 存进 media 列。
    """

    def op(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO chat_log "
            "(group_openid, role, user_openid, username, content, media, "
            " message_id, created_at) "
            "VALUES (?, 'user', ?, ?, ?, ?, ?, ?)",
            (
                group_openid,
                user_openid,
                username,
                content,
                items_to_json(media),
                message_id,
                created_at if created_at is not None else time.time(),
            ),
        )
        conn.commit()

    await _run(op)


async def has_message(group_openid: str, message_id: str) -> bool:
    """该群是否已经记录过这条消息（用于避免重复下载附件）。"""

    def op(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT 1 FROM chat_log "
            "WHERE group_openid = ? AND message_id = ? LIMIT 1",
            (group_openid, message_id),
        ).fetchone()
        return row is not None

    return await _run(op)


async def entry_for_message(
    group_openid: str, message_id: str
) -> ChatLogEntry | None:
    """按 message_id 取一条已记录的群消息（含媒体）；没有则返回 None。"""

    def op(conn: sqlite3.Connection) -> ChatLogEntry | None:
        row = conn.execute(
            "SELECT role, user_openid, username, content, media, created_at "
            "FROM chat_log "
            "WHERE group_openid = ? AND message_id = ? "
            "ORDER BY id LIMIT 1",
            (group_openid, message_id),
        ).fetchone()
        return _entry_from_row(row) if row is not None else None

    return await _run(op)


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


async def recent_entries(
    group_openid: str,
    limit: int,
    *,
    exclude_message_id: str | None = None,
    since: float | None = None,
) -> list[ChatLogEntry]:
    """读取群里最近的聊天记录，按时间正序返回。

    ``exclude_message_id`` 用于排除当前正在处理的这条消息，避免在 agent
    里重复出现；``since`` 是 Unix 时间戳，只取不早于该时间的记录
    （``None`` 表示不限时间）。
    """
    if limit <= 0:
        return []

    def op(conn: sqlite3.Connection) -> list[ChatLogEntry]:
        rows = conn.execute(
            "SELECT role, user_openid, username, content, media, created_at "
            "FROM chat_log "
            "WHERE group_openid = ? "
            "  AND (? IS NULL OR message_id IS NULL OR message_id != ?) "
            "  AND (? IS NULL OR created_at >= ?) "
            "ORDER BY id DESC LIMIT ?",
            (
                group_openid,
                exclude_message_id,
                exclude_message_id,
                since,
                since,
                limit,
            ),
        ).fetchall()
        return [_entry_from_row(row) for row in reversed(rows)]

    return await _run(op)


def stable_id(user_openid: str) -> str:
    """取 openid 末尾几位作为稳定标识，用于区分同名、应付改名。"""
    user_openid = user_openid.strip()
    return user_openid[-_STABLE_ID_LEN:] if user_openid else ""


def speaker_name(entry: ChatLogEntry) -> str:
    """聊天记录里的发言人显示名：用户名 + openid 稳定标识。

    显示为 ``小明#a1b2c3``；缺少用户名时用 ``玩家#a1b2c3``。同一用户改名
    后标识不变，不同用户重名时仍可区分。
    """
    if entry.role == "bot":
        return "bot"
    name = entry.username.strip() or "玩家"
    suffix = stable_id(entry.user_openid)
    return f"{name}#{suffix}" if suffix else name


def format_transcript_line(entry: ChatLogEntry) -> str:
    """把一条聊天记录格式化为单行：用户 | 时间 | 内容。"""
    stamp = datetime.fromtimestamp(entry.created_at).strftime("%Y-%m-%d %H:%M")
    content = " ".join(entry.content.split())
    return f"{speaker_name(entry)} | {stamp} | {content}"


async def members(group_openid: str) -> list[tuple[str, str]]:
    """返回该群里出现过的 ``(user_openid, username)`` 组合。

    同一用户改过昵称会留下多条；按最近发言排序，最新的昵称在前。
    供 @ 解析建立 openid / 昵称到用户名的映射。
    """

    def op(conn: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = conn.execute(
            "SELECT user_openid, username, MAX(id) AS last_id "
            "FROM chat_log "
            "WHERE group_openid = ? AND role = 'user' AND user_openid != '' "
            "GROUP BY user_openid, username "
            "ORDER BY last_id DESC",
            (group_openid,),
        ).fetchall()
        return [
            (row["user_openid"], row["username"]) for row in rows
        ]

    return await _run(op)


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def earliest_timestamp(group_openid: str) -> float | None:
    """返回该群最早一条聊天记录的时间戳；没有记录时返回 None。"""

    def op(conn: sqlite3.Connection) -> float | None:
        row = conn.execute(
            "SELECT MIN(created_at) AS earliest FROM chat_log "
            "WHERE group_openid = ?",
            (group_openid,),
        ).fetchone()
        if row is None or row["earliest"] is None:
            return None
        return float(row["earliest"])

    return await _run(op)


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
        "SELECT role, user_openid, username, content, media, created_at "
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
        return [_entry_from_row(row) for row in reversed(rows)]

    entries = await _run(op)
    logger.debug(
        f"群聊记录检索：group={group_openid} "
        f"keywords={list(keywords)} 命中 {len(entries)} 条"
    )
    return entries