"""agent 可调用的工具集：聊天工具与知识库工具相互独立。

知识库采用 llms.txt + fetch 的形式：
- ``search_knowledge`` 先读 llms.txt 索引，再按 TTL 抓取索引内页面的正文，
  在标题、章节、说明和正文上做全文检索，返回匹配条目、URL 和正文摘要；
  不传 query 时只列出索引条目；
- ``fetch_doc`` 只允许抓取索引中出现过的 URL，返回页面正文。
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import OrderedDict
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx
from nonebot.log import logger

from ..lab_client import report
from . import chat_log, config


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# ─── 当前对话所在的群 ────────────────────────────────────────

# agent loop 期间由 ai/__init__.py 写入，工具用它确定要检索哪个群
_CURRENT_GROUP: ContextVar[str | None] = ContextVar(
    "ai_current_group", default=None
)


def set_group_context(group_openid: str | None) -> Token:
    return _CURRENT_GROUP.set(group_openid or None)


def reset_group_context(token: Token) -> None:
    _CURRENT_GROUP.reset(token)


# ─── 聊天工具 ────────────────────────────────────────────────


async def _get_server_status() -> str:
    """服务器状态文本；与 /状态 指令共用同一套实现和措辞。"""
    return await report.server_status_text()


async def _start_server() -> str:
    """触发一键启动；与 /启动 指令共用同一套实现和措辞。"""
    return await report.start_server_text()


async def _get_current_time() -> str:
    now = datetime.now().astimezone()
    weekdays = "一二三四五六日"
    return (
        f"{now.strftime('%Y-%m-%d %H:%M:%S %z')} "
        f"星期{weekdays[now.weekday()]}"
    )


# ─── 群聊记录检索 ────────────────────────────────────────────


def _normalize_keywords(value: Any) -> list[str]:
    """把模型给的关键词统一成去重后的短词列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        return []

    terms: list[str] = []
    for item in raw:
        for part in _WORD_SPLIT.split(item):
            part = part.strip()
            if part and part not in terms:
                terms.append(part)
    return terms[:8]


# recall_chat 的时间范围：Nd / Nh / Nm，分别表示最近 N 天 / 小时 / 分钟
_RECENT_RE = re.compile(r"^(\d+)\s*([dhm]?)$", re.IGNORECASE)
_RECENT_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "": 86400}
_RECENT_UNIT_LABELS = {"m": "分钟", "h": "小时", "d": "天", "": "天"}
_RECENT_DEFAULT = "1h"


def _parse_recent(value: Any) -> tuple[float | None, str]:
    """解析 Nd / Nh / Nm 时间范围，返回 (since 秒数, 展示文本)。

    since 为 None 表示不限时间；格式无效时抛 ValueError。
    未提供（None）时使用默认 1h；不带单位的纯数字按天处理，0 表示不限时间。
    """
    if value is None:
        raw = _RECENT_DEFAULT
    else:
        raw = str(value).strip().lower().replace(" ", "")
    match = _RECENT_RE.match(raw)
    if not match:
        raise ValueError(
            f"无法解析 {value!r}，请使用 Nd / Nh / Nm 格式（如 30m、12h、7d）"
        )

    amount = int(match.group(1))
    unit = match.group(2)
    seconds = amount * _RECENT_UNIT_SECONDS[unit]
    if seconds <= 0:
        return None, "不限时间"
    try:
        seconds_value = float(seconds)
    except OverflowError:
        raise ValueError(f"时间范围过大：{amount}{unit or 'd'}") from None
    return seconds_value, f"最近 {amount} {_RECENT_UNIT_LABELS[unit]}"


def _format_log_entry(entry: chat_log.ChatLogEntry) -> str:
    stamp = datetime.fromtimestamp(entry.created_at).strftime("%Y-%m-%d %H:%M")
    return f"[{stamp}] {chat_log.speaker_name(entry)}：{entry.content}"


async def _search_chat_log(
    keywords: Any = None,
    recent: Any = _RECENT_DEFAULT,
    limit: Any = 50,
) -> str:
    group_openid = _CURRENT_GROUP.get()
    if not group_openid:
        return "当前对话不在群聊中，无法检索群聊记录。"

    terms = _normalize_keywords(keywords)
    try:
        seconds, scope = _parse_recent(recent)
    except ValueError as e:
        return f"时间范围无效：{e}。"
    try:
        limit_value = int(limit)
    except (TypeError, ValueError):
        limit_value = 50
    limit_value = max(1, min(limit_value, 200))

    if seconds is None:
        since = None
    else:
        try:
            earliest = await chat_log.earliest_timestamp(group_openid)
        except Exception as e:
            logger.warning(f"群聊记录检索失败: {e}")
            return f"群聊记录检索失败：{e.__class__.__name__}"
        if earliest is None:
            return "本群还没有聊天记录，无法按时间范围检索。"
        since = time.time() - seconds
        if since < earliest:
            earliest_text = datetime.fromtimestamp(earliest).strftime(
                "%Y-%m-%d %H:%M"
            )
            return (
                f"时间范围无效：{scope}的起点早于本群最早聊天记录"
                f"（{earliest_text}）。请缩短 recent，或传 0 查询全部记录。"
            )
    keyword_text = "、".join(terms) if terms else "无"

    try:
        entries = await chat_log.search(
            group_openid,
            terms,
            since=since,
            limit=limit_value,
        )
    except Exception as e:
        logger.warning(f"群聊记录检索失败: {e}")
        return f"群聊记录检索失败：{e.__class__.__name__}"

    if not entries:
        return (
            f"没有找到群聊记录（{scope}，关键词：{keyword_text}）。"
            "可以换更短的关键词、去掉部分关键词，或传 0 查询全部记录。"
        )

    body = "\n".join(_format_log_entry(entry) for entry in entries)
    return (
        f"群聊记录（{scope}，关键词：{keyword_text}，共 {len(entries)} 条）：\n"
        f"{body}"
    )


# ─── 知识库：llms.txt 索引 + fetch ───────────────────────────


class KnowledgeError(Exception):
    """知识库索引或页面抓取失败。"""


@dataclass(frozen=True)
class KnowledgeEntry:
    title: str
    url: str
    description: str = ""
    section: str = ""


@dataclass(frozen=True)
class KnowledgeIndex:
    source_url: str
    url: str
    title: str
    entries: tuple[KnowledgeEntry, ...]


@dataclass(frozen=True)
class KnowledgeDoc:
    """索引条目 + 抓取到的正文；正文为空表示该页抓取失败。"""

    entry: KnowledgeEntry
    text: str = ""
    headings: tuple[str, ...] = ()


_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_H2_RE = re.compile(r"^#{2,6}\s+(.+?)\s*$")
_BLOCKQUOTE_RE = re.compile(r"^>\s?(.*)$")
_LINK_RE = re.compile(
    r"^\s*[-*]\s+\[([^\]]+)\]\(([^)\s]+)"
    r"(?:\s+[\"'][^\"']*[\"'])?\)\s*"
    r"(?:(?::|：|[-–—])\s*)?(.*)$"
)
_CJK_RUN = re.compile(r"[一-鿿]+")
_WORD_SPLIT = re.compile(
    r"[\s,，。.!！?？、;；:：/\\|()（）\[\]【】\"'“”‘’]+"
)

_INDEX_CACHE: tuple[float, KnowledgeIndex] | None = None
_INDEX_LOCK = asyncio.Lock()

# 全量正文缓存：search_knowledge 需要在正文里检索，而知识库页面数量很少，
# 因此按 TTL 并发抓取索引中的全部页面，缓存 (时间, 索引 URL, 条目 URL 列表, 正文)
_DOCS_CACHE: (
    "tuple[float, str, tuple[str, ...], tuple[KnowledgeDoc, ...]] | None"
) = None
_DOCS_LOCK = asyncio.Lock()

_PAGE_CACHE: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_PAGE_CACHE_MAX = 64

_USER_AGENT = "SeatideBot/0.1 (+knowledge-llms-txt)"
_ACCEPT = (
    "text/markdown, text/plain, text/html;q=0.9, "
    "application/xhtml+xml;q=0.8, */*;q=0.1"
)
_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_RAW_TEXT_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/x-markdown",
    "application/json",
    "application/yaml",
    "text/yaml",
}


def _plain_preview(text: str) -> str:
    cleaned = re.sub(r"```.*?```", " ", text, flags=re.S)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.M)
    cleaned = re.sub(r"[*_>`~-]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _parse_llms_txt(text: str, base_url: str) -> tuple[str, list[KnowledgeEntry]]:
    """解析 llms.txt 中的 Markdown 链接列表。

    标准格式：
        # 标题
        > 简介
        ## 章节
        - [页面标题](https://example.com/page.md): 页面说明
    """
    title = ""
    summary_lines: list[str] = []
    section = ""
    entries: list[KnowledgeEntry] = []
    seen: set[str] = set()
    in_header = True

    for raw_line in text.splitlines():
        line = raw_line.lstrip("﻿")
        stripped = line.strip()

        h1 = _H1_RE.match(stripped)
        if h1:
            if not title:
                title = h1.group(1).strip()
            continue

        h2 = _H2_RE.match(stripped)
        if h2:
            section = h2.group(1).strip()
            in_header = False
            continue

        if stripped.startswith(">"):
            if in_header and not entries:
                quote = _BLOCKQUOTE_RE.match(stripped)
                if quote:
                    summary_lines.append(quote.group(1).strip())
            continue

        match = _LINK_RE.match(line)
        if not match:
            continue

        entry_title = match.group(1).strip()
        href = match.group(2).strip()
        description = match.group(3).strip()
        url = urljoin(base_url, href).split("#", 1)[0]
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if url in seen:
            continue
        seen.add(url)
        entries.append(
            KnowledgeEntry(
                title=entry_title,
                url=url,
                description=description,
                section=section,
            )
        )

    # 没有链接时，把非空的 llms.txt 本身当作一份可直接 fetch 的文档
    if not entries and text.strip():
        summary = " ".join(part for part in summary_lines if part).strip()
        if not summary:
            summary = _plain_preview(text)[:800]
        entries.append(
            KnowledgeEntry(
                title=title or "知识库",
                url=base_url.split("#", 1)[0],
                description=summary,
                section="",
            )
        )

    return title, entries


def _ensure_http_url(url: str) -> None:
    """禁止 http(s) 之外的协议。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise KnowledgeError("只允许 http/https URL")
    if not parsed.hostname:
        raise KnowledgeError("URL 缺少主机名")


_FETCH_REGEX_CACHE: "tuple[str, re.Pattern[str]] | None" = None


def _ensure_fetch_url_allowed(url: str) -> None:
    """校验 URL 是否匹配 AI_KNOWLEDGE_FETCH_URL_REGEX（完整匹配）。

    正则留空时不做额外限制，只受 llms.txt 索引约束。
    """
    global _FETCH_REGEX_CACHE

    raw = config.AI_KNOWLEDGE_FETCH_URL_REGEX.strip()
    if not raw:
        return

    cached = _FETCH_REGEX_CACHE
    if cached is not None and cached[0] == raw:
        pattern = cached[1]
    else:
        try:
            pattern = re.compile(raw)
        except re.error as e:
            raise KnowledgeError(
                f"AI_KNOWLEDGE_FETCH_URL_REGEX 不是合法正则：{e}"
            ) from e
        _FETCH_REGEX_CACHE = (raw, pattern)

    if pattern.fullmatch(url) is None:
        raise KnowledgeError(
            "URL 不在允许抓取的范围内（AI_KNOWLEDGE_FETCH_URL_REGEX）："
            f"{url}"
        )


class _HTMLTextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript",
                  "svg", "nav", "footer", "aside"}
    _BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "main",
        "pre",
        "blockquote",
        "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth == 0:
            self.parts.append(data)


def _html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass

    text = "".join(parser.parts)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()
    if title:
        return f"标题：{title}\n\n{text}"
    return text


def _decode_body(
    raw: bytes, encoding: str, content_type: str
) -> str:
    try:
        text = raw.decode(encoding or "utf-8", errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")

    lowered_type = content_type.lower()
    if lowered_type in _HTML_CONTENT_TYPES:
        return _html_to_text(text)
    if not lowered_type and "<html" in text[:500].lower():
        return _html_to_text(text)
    if lowered_type in _RAW_TEXT_CONTENT_TYPES or lowered_type.startswith("text/"):
        return text
    raise KnowledgeError(f"不支持的内容类型：{content_type or '未知'}")


async def _fetch_document(
    url: str,
    timeout: float,
    *,
    url_check: Callable[[str], None] | None = None,
) -> tuple[str, str]:
    """抓取文本/Markdown/HTML 页面，手动校验每一次重定向。

    ``url_check`` 会在请求的每一跳上执行，避免重定向绕过白名单。
    """
    current = url.strip()
    headers = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), follow_redirects=False
        ) as client:
            for _ in range(4):
                _ensure_http_url(current)
                if url_check is not None:
                    url_check(current)
                async with client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in _REDIRECT_STATUS:
                        location = resp.headers.get("location")
                        if not location:
                            raise KnowledgeError("重定向缺少 Location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise KnowledgeError(
                            f"HTTP {resp.status_code}：{current}")

                    content_type = (
                        (resp.headers.get("content-type") or "")
                        .split(";", 1)[0]
                        .strip()
                        .lower()
                    )
                    raw = await resp.aread()
                    encoding = resp.encoding or "utf-8"
                    return _decode_body(raw, encoding, content_type), current
    except httpx.TimeoutException as e:
        raise KnowledgeError("请求超时") from e
    except httpx.HTTPError as e:
        raise KnowledgeError(f"网络错误：{e.__class__.__name__}") from e

    raise KnowledgeError("重定向次数过多")


async def _get_index() -> KnowledgeIndex:
    global _INDEX_CACHE

    url = config.AI_KNOWLEDGE_LLMS_TXT_URL.strip()
    if not url:
        raise KnowledgeError(
            "知识库未配置（AI_KNOWLEDGE_LLMS_TXT_URL 为空）"
        )

    ttl = config.AI_KNOWLEDGE_CACHE_TTL
    now = time.monotonic()
    cached = _INDEX_CACHE
    if (
        cached is not None
        and cached[1].source_url == url
        and ttl > 0
        and now - cached[0] < ttl
    ):
        return cached[1]

    async with _INDEX_LOCK:
        now = time.monotonic()
        cached = _INDEX_CACHE
        if (
            cached is not None
            and cached[1].source_url == url
            and ttl > 0
            and now - cached[0] < ttl
        ):
            return cached[1]

        text, final_url = await _fetch_document(
            url,
            config.AI_KNOWLEDGE_FETCH_TIMEOUT,
        )

        title, entries = _parse_llms_txt(text, final_url)
        index = KnowledgeIndex(
            source_url=url,
            url=final_url,
            title=title,
            entries=tuple(entries),
        )
        _INDEX_CACHE = (now, index)
        logger.info(
            f"知识库索引已加载：{len(entries)} 条（{final_url}）"
        )
        return index


async def check_knowledge_index() -> KnowledgeIndex:
    """启动时检查 llms.txt 能否获取、解析出条目。"""
    index = await _get_index()
    if not index.entries:
        raise KnowledgeError("llms.txt 没有解析到任何条目")
    return index


async def _get_page(url: str) -> str:
    ttl = config.AI_KNOWLEDGE_CACHE_TTL
    now = time.monotonic()
    cached = _PAGE_CACHE.get(url)
    if cached is not None and ttl > 0 and now - cached[0] < ttl:
        _PAGE_CACHE.move_to_end(url)
        return cached[1]

    text, _ = await _fetch_document(
        url,
        config.AI_KNOWLEDGE_FETCH_TIMEOUT,
        url_check=_ensure_fetch_url_allowed,
    )
    if ttl > 0:
        _PAGE_CACHE[url] = (now, text)
        _PAGE_CACHE.move_to_end(url)
        while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
            _PAGE_CACHE.popitem(last=False)
    return text


_MD_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _normalize_doc_text(text: str) -> str:
    """压缩正文中的多余空白，保留段落结构供摘要截取。"""
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _doc_headings(text: str) -> tuple[str, ...]:
    """提取 Markdown 标题；标题命中通常比正文命中更能说明页面主题。"""
    return tuple(
        match.strip() for match in _MD_HEADING_RE.findall(text) if match.strip()
    )


async def _load_doc(entry: KnowledgeEntry) -> KnowledgeDoc:
    try:
        text = await _get_page(entry.url)
    except KnowledgeError as e:
        logger.warning(f"知识库正文获取失败：{entry.url}（{e}）")
        return KnowledgeDoc(entry=entry)
    except Exception as e:  # pragma: no cover - 防御性兜底
        logger.warning(
            f"知识库正文获取失败：{entry.url}"
            f"（{e.__class__.__name__}: {e}）"
        )
        return KnowledgeDoc(entry=entry)
    normalized = _normalize_doc_text(text)
    return KnowledgeDoc(
        entry=entry,
        text=normalized,
        headings=_doc_headings(normalized),
    )


async def _get_docs(
    index: KnowledgeIndex,
) -> tuple[KnowledgeDoc, ...]:
    """按 TTL 抓取索引中全部页面的正文，供全文检索使用。"""
    global _DOCS_CACHE

    ttl = config.AI_KNOWLEDGE_CACHE_TTL
    urls = tuple(entry.url for entry in index.entries)

    def _cached() -> tuple[KnowledgeDoc, ...] | None:
        cached = _DOCS_CACHE
        if (
            cached is not None
            and cached[1] == index.source_url
            and cached[2] == urls
            and ttl > 0
            and time.monotonic() - cached[0] < ttl
        ):
            return cached[3]
        return None

    docs = _cached()
    if docs is not None:
        return docs

    async with _DOCS_LOCK:
        docs = _cached()
        if docs is not None:
            return docs

        docs = tuple(
            await asyncio.gather(
                *(_load_doc(entry) for entry in index.entries)
            )
        )
        _DOCS_CACHE = (
            time.monotonic(),
            index.source_url,
            urls,
            docs,
        )
        loaded = sum(1 for doc in docs if doc.text)
        logger.info(
            f"知识库正文已加载：{loaded}/{len(docs)} 篇"
            f"（{index.source_url}）"
        )
        return docs


# 问句虚词和功能词不参与打分，否则「如何申请白名单」会被「如何开启服务器」
# 这类只有虚词重合的条目抢走
_QUERY_STOPWORDS = frozenset(
    {
        "如何",
        "怎么",
        "怎样",
        "什么",
        "哪些",
        "哪里",
        "哪个",
        "为何",
        "是否",
        "可以",
        "能否",
        "请问",
        "需要",
        "一个",
        "一下",
        "有没有",
        "没有",
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "这里",
        "那里",
        "问题",
        "关于",
        "有关",
        "以及",
        "还是",
        "或者",
        "但是",
        "因为",
        "如果",
        "已经",
        "就是",
        "不是",
        "自己",
        "时候",
        "吗",
        "呢",
        "吧",
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "或",
        # 英文问句与功能词
        "how",
        "what",
        "why",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "will",
        "shall",
        "may",
        "might",
        "must",
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "and",
        "or",
        "but",
        "not",
        "my",
        "me",
        "i",
        "you",
        "we",
        "they",
        "it",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "please",
        "with",
        "about",
        "from",
        "have",
        "has",
        "had",
    }
)

_SNIPPET_BEFORE = 80
_SNIPPET_AFTER = 160


def _query_terms(query: str) -> dict[str, float]:
    """把查询拆成带权重的检索词；中文额外拆成二元组。

    完整词权重高于二元组，保证「白名单」这类专有词优先命中。
    """
    text = query.strip().lower()
    if not text:
        return {}

    terms: dict[str, float] = {}

    def add(term: str, weight: float) -> None:
        term = term.strip()
        if len(term) < 2 or term in _QUERY_STOPWORDS:
            return
        if weight > terms.get(term, 0.0):
            terms[term] = weight

    for token in _WORD_SPLIT.split(text):
        add(token, 2.0)

    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            if run not in _QUERY_STOPWORDS:
                terms[run] = max(terms.get(run, 0.0), 0.5)
            continue
        for i in range(len(run) - 1):
            add(run[i: i + 2], 1.0)

    return terms


def _doc_snippet(doc: KnowledgeDoc, matched: dict[str, float]) -> str:
    """截取正文中命中检索词的位置附近的内容作为摘要。"""
    body = doc.text
    if not body:
        return _plain_preview(doc.entry.description)[:300]

    lowered = body.lower()
    positions: list[tuple[float, int]] = []
    for term, weight in matched.items():
        pos = lowered.find(term)
        if pos < 0:
            continue
        positions.append((weight * min(lowered.count(term), 6), pos))
    if not positions:
        return _plain_preview(doc.entry.description)[:300]

    positions.sort(reverse=True)
    spans: list[list[int]] = []
    for _, pos in positions[:2]:
        start = max(0, pos - _SNIPPET_BEFORE)
        # 尽量从最近的标题或段落开头截取，避免摘要从半句话开始
        heading = body.rfind("\n#", start, pos)
        if heading >= start:
            start = heading + 1
        else:
            paragraph = body.rfind("\n\n", start, pos)
            if paragraph >= start:
                start = paragraph + 2
        spans.append(
            [start, min(len(body), pos + _SNIPPET_AFTER)]
        )
    spans.sort()

    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 40:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    parts: list[str] = []
    for start, end in merged:
        part = re.sub(r"\s+", " ", body[start:end]).strip()
        if not part:
            continue
        if start > 0:
            part = "…" + part
        if end < len(body):
            part = part + "…"
        parts.append(part)
    return " ".join(parts)


def _search_docs(
    docs: tuple[KnowledgeDoc, ...], query: str, top_k: int
) -> list[tuple[KnowledgeDoc, str]]:
    """在标题、章节、说明和正文上检索，返回 (文档, 摘要)。"""
    terms = _query_terms(query)
    if not terms or not docs:
        return []

    views = [
        (
            doc,
            doc.entry.title.lower(),
            doc.entry.section.lower(),
            doc.entry.description.lower(),
            doc.entry.url.lower(),
            "\n".join(doc.headings).lower(),
            doc.text.lower(),
        )
        for doc in docs
    ]

    # 在越少文档中出现的词越有区分度（IDF），避免「服务器」这类高频词压过
    # 「白名单」这类真正决定相关性的词
    total = len(views)
    weights: dict[str, float] = {}
    for term, base_weight in terms.items():
        df = sum(
            1
            for _, title, section, description, url, headings, body in views
            if (
                term in title
                or term in section
                or term in description
                or term in url
                or term in headings
                or term in body
            )
        )
        if df == 0:
            continue
        idf = math.log((total + 1) / (df + 0.5)) + 1.0
        weights[term] = base_weight * idf
    if not weights:
        return []

    scored: list[tuple[float, KnowledgeDoc, dict[str, float]]] = []
    for doc, title, section, description, url, headings, body in views:
        score = 0.0
        matched: dict[str, float] = {}
        for term, weight in weights.items():
            hits = (
                min(title.count(term), 3) * 5.0
                + min(section.count(term), 2) * 2.0
                + min(description.count(term), 2) * 2.0
                + min(url.count(term), 2) * 2.0
                + min(headings.count(term), 3) * 3.0
                + min(body.count(term), 6)
            )
            if hits:
                score += weight * hits
                matched[term] = weight
        if score > 0:
            scored.append((score, doc, matched))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [
        (doc, _doc_snippet(doc, matched))
        for _, doc, matched in scored[:top_k]
    ]


_GENERIC_SECTIONS = {"table of contents", "contents", "目录", "索引"}


def _display_section(section: str) -> str:
    """过滤 llms.txt 里没有信息量的通用章节名。"""
    return "" if section.strip().lower() in _GENERIC_SECTIONS else section


def _format_entry(index: int, entry: KnowledgeEntry) -> str:
    lines = [f"{index}. {entry.title}"]
    section = _display_section(entry.section)
    if section:
        lines.append(f"   章节：{section}")
    lines.append(f"   URL：{entry.url}")
    if entry.description:
        lines.append(f"   说明：{entry.description[:300]}")
    return "\n".join(lines)


def _format_search_result(index: int, doc: KnowledgeDoc, snippet: str) -> str:
    lines = [f"{index}. {doc.entry.title}"]
    section = _display_section(doc.entry.section)
    if section:
        lines.append(f"   章节：{section}")
    lines.append(f"   URL：{doc.entry.url}")
    if snippet:
        lines.append(f"   摘要：{snippet}")
    elif doc.entry.description:
        lines.append(f"   说明：{doc.entry.description[:300]}")
    return "\n".join(lines)


_DEFAULT_SEARCH_LIMIT = 5


def _normalize_limit(value: object) -> int | None:
    """把 search_knowledge 的 limit 参数转成正整数。

    未提供（None / 空字符串）返回 None；非法值抛出 ValueError。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = int(raw)
        except ValueError:
            raise ValueError from None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError
        parsed = int(value)
    else:
        raise ValueError
    if parsed < 1:
        raise ValueError
    return parsed


async def _search_knowledge(
    query: str | None = None, limit: int | None = None
) -> str:
    query = str(query or "").strip()
    try:
        cap = _normalize_limit(limit)
    except ValueError:
        return (
            f"limit 参数无效：{limit!r} 不是大于 0 的整数。"
            "请传正整数，或省略 limit 使用默认值。"
        )

    try:
        index = await _get_index()
    except KnowledgeError as e:
        return (
            f"知识库索引获取失败：{e}。"
            "请告知用户暂时无法查询知识库，并建议询问服主。"
        )

    if not index.entries:
        return "知识库索引为空，请告知用户暂时无法查询知识库，并建议询问服主。"

    # 不传 query 时列出条目：默认返回全部（当前知识库页面不多），
    # 传了 limit 则只截取前 limit 条。
    if not query:
        entries = index.entries if cap is None else index.entries[:cap]
        body = "\n\n".join(
            _format_entry(i + 1, entry)
            for i, entry in enumerate(entries)
        )
        scope = (
            f"全部 {len(index.entries)} 条"
            if len(entries) >= len(index.entries)
            else f"前 {len(entries)} 条，共 {len(index.entries)} 条"
        )
        return (
            f"知识库条目（{scope}，来自 {index.url}）：\n{body}\n\n"
            "如需正文，请用 fetch_doc 抓取上面的 URL。"
        )

    docs = await _get_docs(index)
    matches = _search_docs(
        docs, query, cap or _DEFAULT_SEARCH_LIMIT
    )
    if not matches:
        available = "\n".join(
            f"- {entry.title}：{entry.url}"
            for entry in index.entries[:10]
        )
        return (
            f"知识库正文中没有匹配「{query}」的内容。"
            f"可用条目（前 10 条）：\n{available}\n"
            "可以换个关键词再次检索，或直接 fetch 看起来相关的条目。"
        )

    body = "\n\n".join(
        _format_search_result(i + 1, doc, snippet)
        for i, (doc, snippet) in enumerate(matches)
    )
    return (
        f"知识库全文检索结果（来自 {index.url}）：\n{body}\n\n"
        "摘要已包含正文相关内容时可直接作答；"
        "需要更多细节再用 fetch_doc 抓取上面的 URL。"
    )


async def _fetch_doc(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return "URL 为空。"

    try:
        index = await _get_index()
    except KnowledgeError as e:
        return f"知识库索引获取失败：{e}。"

    allowed = {entry.url for entry in index.entries}
    allowed.add(index.url)
    allowed.add(index.source_url)

    normalized = urljoin(index.url, raw).split("#", 1)[0]
    if normalized not in allowed:
        return (
            "只能抓取 search_knowledge 返回的 llms.txt 条目 URL。"
            "请先调用 search_knowledge 获取可用链接。"
        )

    try:
        _ensure_fetch_url_allowed(normalized)
    except KnowledgeError as e:
        logger.warning(f"知识库页面抓取被拒绝：{normalized}（{e}）")
        return f"抓取被拒绝：{e}"

    try:
        text = await _get_page(normalized)
    except KnowledgeError as e:
        logger.warning(f"知识库页面抓取失败：{normalized}（{e}）")
        return f"抓取失败：{e}"

    return f"URL：{normalized}\n\n{text}"


# ─── 工具注册 ────────────────────────────────────────────────

GET_SERVER_STATUS = Tool(
    name="get_server_status",
    description=(
        "查询 Seatide Minecraft 服务器的实时状态：是否在线、IP、玩家数、"
        "运行时长、内存/CPU 占用和预计释放时间。"
    ),
    parameters={"type": "object", "properties": {}},
    handler=_get_server_status,
)

START_SERVER = Tool(
    name="start_server",
    description=(
        "触发 Seatide 服务器的一键启动，"
        "只在玩家明确要求现在立即启动或开服时调用，其余情况请先确认。"
    ),
    parameters={"type": "object", "properties": {}},
    handler=_start_server,
)

GET_CURRENT_TIME = Tool(
    name="get_current_time",
    description="获取当前日期、时间和星期。",
    parameters={"type": "object", "properties": {}},
    handler=_get_current_time,
)

RECALL_CHAT = Tool(
    name="recall_chat",
    description=(
        "按关键词检索本群存量聊天记录"
    ),
    parameters={
        "type": "object",
        "properties": {
            "keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "关键词列表，每个词都要在消息内容中出现才算命中；"
                    "中文建议用 2-4 字的短词。不传则只按时间检索。"
                ),
            },
            "recent": {
                "type": "string",
                "description": (
                    "时间范围，格式为 Nd / Nh / Nm，分别表示最近 N 天 / "
                    "小时 / 分钟，默认 1h；传 0 表示不限时间。"
                    "范围早于本群最早聊天记录时会报错"
                ),
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少条，默认 50，最大 200",
            },
        },
    },
    handler=_search_chat_log,
)

SEARCH_KNOWLEDGE = Tool(
    name="search_knowledge",
    description=(
        "检索服务器知识库：对文章标题、说明和正文做全文匹配，"
        "返回匹配条目、链接和正文摘要；不传 query 时列出全部条目"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "关键内容词或完整问题（如「白名单」「开启服务器」）；"
                    "避免只传「如何」「怎么」这类虚词；"
                    "不填则返回知识库全部条目"
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "最多返回多少条，必须是大于 0 的整数；"
                    "按关键词检索时默认 5，不传 query 时默认返回全部"
                ),
            },
        },
    },
    handler=_search_knowledge,
)

FETCH_DOC = Tool(
    name="fetch_doc",
    description=(
        "获取知识库链接对应正文"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "完整知识库文档 URL",
            }
        },
        "required": ["url"],
    },
    handler=_fetch_doc,
)

# 普通聊天 agent 只负责回忆群聊记录；实时状态、当前时间交给 knowledge
CHAT_TOOLS: tuple[Tool, ...] = (RECALL_CHAT,)

# 知识库问答 agent：检索 llms.txt → fetch 正文，也负责实时状态和当前时间，
# 并能像 /启动 指令一样触发一键启动
KNOWLEDGE_TOOLS: tuple[Tool, ...] = (
    SEARCH_KNOWLEDGE,
    FETCH_DOC,
    GET_SERVER_STATUS,
    START_SERVER,
    GET_CURRENT_TIME,
    RECALL_CHAT
)
