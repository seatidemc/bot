"""解析 agent 回复中的 @，转换成 QQ 的 mention 段。

聊天记录里的发言人显示为 ``昵称#标识``（标识是 ``user_openid`` 后 6 位）。
模型可以在回复里写：

- ``@昵称``：昵称唯一时定位到这个用户；
- ``@昵称#标识``：昵称重名、改过名或不确定是谁时，用标识定位；
- ``@标识``：只写标识也能定位。

解析成功的 @ 会替换成 ``MessageSegment.mention_user``，群里显示为真实 @；
解析不到时保留 ``@昵称`` 文本，并且不把 ``#标识`` 原样发出去。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from nonebot.adapters.qq import Message, MessageSegment

from . import chat_log


@dataclass(frozen=True)
class MentionDirectory:
    """本群成员的定位表；重名的昵称和冲突的标识会被排除。"""

    by_id: dict[str, str]
    by_name: dict[str, str]
    display_name: dict[str, str]
    pattern: re.Pattern[str] | None

    def resolve(self, match: re.Match[str]) -> tuple[str, str] | None:
        """把一次 @ 匹配解析成 ``(完整 openid, 显示用昵称)``。"""
        anyname = match.group("anyname")
        if anyname is not None:
            # 昵称不在记录里（模型写错或新昵称）时，标识仍然可信
            openid = self.by_id.get(
                (match.group("anyident") or "").upper()
            )
            if openid is None:
                return None
            display = self.display_name.get(openid, "") or anyname
            return openid, display

        bare = match.group("bare")
        if bare:
            openid = self.by_id.get(bare.upper())
            if openid is None:
                return None
            return openid, self.display_name.get(openid, "")

        name = match.group("name") or ""
        ident = match.group("ident")
        if ident:
            openid = self.by_id.get(ident.upper())
            if openid is not None:
                return openid, name
        openid = self.by_name.get(name)
        if openid is None:
            return None
        return openid, name


@dataclass(frozen=True)
class RenderedReply:
    """解析结果：``text`` 是不带标识的纯文本，``message`` 含有 @ 段。"""

    text: str
    message: Message | None = None
    mentions: int = 0


def build(rows: Iterable[tuple[str, str]]) -> MentionDirectory:
    """根据 ``chat_log.members()`` 的 ``(openid, username)`` 建立定位表。"""
    by_id: dict[str, str] = {}
    ambiguous_ids: set[str] = set()
    by_name: dict[str, str] = {}
    ambiguous_names: set[str] = set()
    display_name: dict[str, str] = {}
    all_names: set[str] = set()

    # rows 按最近发言排序，先出现的昵称就是该用户最新的昵称
    for openid, username in rows:
        openid = (openid or "").strip()
        if not openid:
            continue

        tag = chat_log.stable_id(openid).upper()
        if tag:
            previous = by_id.get(tag)
            if previous is None:
                by_id[tag] = openid
            elif previous != openid:
                ambiguous_ids.add(tag)

        name = (username or "").strip() or "玩家"
        display_name.setdefault(openid, name)
        all_names.add(name)
        previous = by_name.get(name)
        if previous is None:
            by_name[name] = openid
        elif previous != openid:
            ambiguous_names.add(name)

    for tag in ambiguous_ids:
        by_id.pop(tag, None)
    for name in ambiguous_names:
        by_name.pop(name, None)

    return MentionDirectory(
        by_id=by_id,
        by_name=by_name,
        display_name=display_name,
        pattern=_compile_pattern(all_names),
    )


def _compile_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    ordered = sorted(set(names), key=len, reverse=True)
    alternatives: list[str] = []
    if ordered:
        joined = "|".join(re.escape(name) for name in ordered)
        alternatives.append(
            rf"(?P<name>{joined})"
            rf"(?:#(?P<ident>[A-Za-z0-9]{{6,}})(?![A-Za-z0-9]))?"
        )
    # 昵称不在记录里（模型写错）时，只要标识有效仍然按标识定位
    alternatives.append(
        r"(?P<anyname>[^\s@#]{1,32})"
        r"#(?P<anyident>[A-Za-z0-9]{6,})(?![A-Za-z0-9])"
    )
    # 只写标识时也允许 # 前缀，例如 @DD4444 / @#DD4444
    alternatives.append(
        r"#?(?P<bare>[A-Za-z0-9]{6})(?![A-Za-z0-9])"
    )
    # @ 前面不能紧跟 ASCII 字母/数字等，避免把邮箱地址里的 @ 当成人名
    return re.compile(
        r"(?<![A-Za-z0-9_.])@(?:" + "|".join(alternatives) + ")"
    )


def render(
    text: str, directory: MentionDirectory | None
) -> RenderedReply:
    """把回复里的 @ 替换成 mention 段；没有可解析的 @ 时原样返回。"""
    if directory is None or directory.pattern is None or "@" not in text:
        return RenderedReply(text)

    segments: list[MessageSegment] = []
    pos = 0
    resolved = 0
    sanitized = False

    def add_text(chunk: str) -> None:
        if chunk:
            segments.append(MessageSegment.text(chunk))

    for match in directory.pattern.finditer(text):
        target = directory.resolve(match)
        if target is None:
            # 标识对不上：去掉 #标识，只保留 @昵称
            name = match.group("name") or match.group("anyname")
            has_ident = match.group("ident") or match.group("anyident")
            if name and has_ident:
                add_text(text[pos : match.start()])
                add_text(f"@{name}")
                pos = match.end()
                sanitized = True
            continue

        openid, name = target
        add_text(text[pos : match.start()])
        segments.append(MessageSegment.mention_user(openid, name or None))
        pos = match.end()
        resolved += 1

    if not resolved and not sanitized:
        return RenderedReply(text)

    add_text(text[pos:])
    message = Message(segments)
    plain = "".join(
        segment.data["text"]
        if segment.type == "text"
        else "@" + str(segment.data.get("username") or "")
        for segment in message
    )
    return RenderedReply(
        text=plain,
        message=message if resolved else None,
        mentions=resolved,
    )