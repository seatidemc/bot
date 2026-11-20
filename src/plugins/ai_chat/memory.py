"""进程内多轮对话记忆：按用户保存最近 N 轮，超时或重启后清空。"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from . import config

# key -> {"messages": [...], "updated": 时间戳}
_STORE: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_MAX_USERS = 1000


def _trim(key: str) -> None:
    entry = _STORE.get(key)
    if not entry:
        return
    limit = max(1, config.AI_MAX_TURNS) * 2
    msgs = entry["messages"]
    if len(msgs) > limit:
        del msgs[: len(msgs) - limit]


def _evict_expired() -> None:
    ttl = config.AI_HISTORY_TTL
    if ttl <= 0:
        return
    now = time.time()
    expired = [k for k, v in _STORE.items() if now - v["updated"] > ttl]
    for k in expired:
        _STORE.pop(k, None)


def get_messages(key: str) -> list[dict[str, str]]:
    _evict_expired()
    entry = _STORE.get(key)
    if not entry:
        return []
    entry["updated"] = time.time()
    _STORE.move_to_end(key)
    return list(entry["messages"])


def add_turn(key: str, user_text: str, assistant_text: str) -> None:
    entry = _STORE.get(key)
    if entry is None:
        entry = {"messages": [], "updated": time.time()}
        _STORE[key] = entry
    entry["messages"].append({"role": "user", "content": user_text})
    entry["messages"].append({"role": "assistant", "content": assistant_text})
    entry["updated"] = time.time()
    _STORE.move_to_end(key)
    _trim(key)
    while len(_STORE) > _MAX_USERS:
        _STORE.popitem(last=False)
