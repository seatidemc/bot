"""SQLite 持久化：存储 QQ 号到 JWT 的映射。"""

import asyncio
import time

import aiosqlite

_DB: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


async def init(db_path: str = "data/bot.db") -> None:
    global _DB
    _DB = await aiosqlite.connect(db_path)
    _DB.row_factory = aiosqlite.Row
    await _DB.execute("PRAGMA journal_mode=WAL")
    await _DB.execute("PRAGMA foreign_keys=ON")
    await _DB.execute(
        """
        CREATE TABLE IF NOT EXISTS user_tokens (
            qq_number   TEXT PRIMARY KEY,
            username    TEXT NOT NULL,
            jwt_token   TEXT NOT NULL,
            jwt_exp_ts  INTEGER NOT NULL,
            whitelist_uuid TEXT,
            created_at  INTEGER NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    await _DB.commit()


async def close() -> None:
    if _DB:
        await _DB.close()


async def save_token(
    qq: str,
    username: str,
    jwt: str,
    exp_ts: int,
    whitelist_uuid: str | None = None,
) -> None:
    async with _lock:
        await _DB.execute(
            """
            INSERT OR REPLACE INTO user_tokens
              (qq_number, username, jwt_token, jwt_exp_ts, whitelist_uuid, created_at)
            VALUES (?, ?, ?, ?, ?, unixepoch())
            """,
            (qq, username, jwt, exp_ts, whitelist_uuid),
        )
        await _DB.commit()


async def get_token(qq: str) -> dict | None:
    async with _lock:
        cursor = await _DB.execute(
            "SELECT * FROM user_tokens WHERE qq_number = ?", (qq,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None

        # 检查是否过期
        if row["jwt_exp_ts"] < int(time.time()):
            await _DB.execute(
                "DELETE FROM user_tokens WHERE qq_number = ?", (qq,)
            )
            await _DB.commit()
            return None

        return dict(row)
