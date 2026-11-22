"""agent 可调用的工具集：聊天工具与知识库工具相互独立。

知识库采用 llms.txt + fetch 的形式：
- ``search_knowledge`` 拉取并检索 llms.txt 索引，返回匹配条目的标题、URL 和说明；
- ``fetch_url`` 只允许抓取索引中出现过的 URL，返回页面正文。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

import httpx
from nonebot.log import logger

from ..lab_client import api as backend_api
from . import config


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


# ─── 聊天工具 ────────────────────────────────────────────────


async def _get_server_status() -> str:
    """查询后端服务器状态并返回原始 JSON。"""
    try:
        data = await backend_api.get_server_status()
    except backend_api.APIError as e:
        return f"查询服务器状态失败：{e.message}"
    except Exception as e:  # pragma: no cover - 防御性兜底
        return f"查询服务器状态失败：{e.__class__.__name__}: {e}"
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def _get_current_time() -> str:
    now = datetime.now().astimezone()
    weekdays = "一二三四五六日"
    return (
        f"{now.strftime('%Y-%m-%d %H:%M:%S %z')} "
        f"星期{weekdays[now.weekday()]}"
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


class _HTMLTextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside"}
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


async def _fetch_document(url: str, timeout: float) -> tuple[str, str]:
    """抓取文本/Markdown/HTML 页面，手动校验每一次重定向。"""
    current = url.strip()
    headers = {"User-Agent": _USER_AGENT, "Accept": _ACCEPT}

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), follow_redirects=False
        ) as client:
            for _ in range(4):
                _ensure_http_url(current)
                async with client.stream("GET", current, headers=headers) as resp:
                    if resp.status_code in _REDIRECT_STATUS:
                        location = resp.headers.get("location")
                        if not location:
                            raise KnowledgeError("重定向缺少 Location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise KnowledgeError(f"HTTP {resp.status_code}：{current}")

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
    cached = _INDEX_CACHE[1] if _INDEX_CACHE else None
    if (
        cached is not None
        and cached.source_url == url
        and ttl > 0
        and now - _INDEX_CACHE[0] < ttl
    ):
        return cached

    async with _INDEX_LOCK:
        now = time.monotonic()
        cached = _INDEX_CACHE[1] if _INDEX_CACHE else None
        if (
            cached is not None
            and cached.source_url == url
            and ttl > 0
            and now - _INDEX_CACHE[0] < ttl
        ):
            return cached

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
    )
    if ttl > 0:
        _PAGE_CACHE[url] = (now, text)
        _PAGE_CACHE.move_to_end(url)
        while len(_PAGE_CACHE) > _PAGE_CACHE_MAX:
            _PAGE_CACHE.popitem(last=False)
    return text


def _query_terms(query: str) -> set[str]:
    """把查询拆成关键词；中文额外拆成二元组，避免没有空格时无法命中。"""
    text = query.strip().lower()
    if not text:
        return set()

    terms: set[str] = set()
    for token in _WORD_SPLIT.split(text):
        token = token.strip()
        if len(token) >= 2:
            terms.add(token)

    for run in _CJK_RUN.findall(text):
        if len(run) == 1:
            terms.add(run)
        for i in range(len(run) - 1):
            terms.add(run[i : i + 2])

    if 2 <= len(text) <= 40:
        terms.add(text)
    return terms


def _search_entries(
    entries: tuple[KnowledgeEntry, ...], query: str, top_k: int
) -> list[KnowledgeEntry]:
    terms = _query_terms(query)
    if not terms:
        return []

    scored: list[tuple[float, KnowledgeEntry]] = []
    for entry in entries:
        title = entry.title.lower()
        section = entry.section.lower()
        description = entry.description.lower()
        url = entry.url.lower()
        score = 0.0
        for term in terms:
            if term in title:
                score += 4.0
            if term in section:
                score += 2.0
            if term in description:
                score += 2.0
            if term in url:
                score += 1.0
        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in scored[:top_k]]


def _format_entry(index: int, entry: KnowledgeEntry) -> str:
    lines = [f"{index}. {entry.title}"]
    if entry.section:
        lines.append(f"   章节：{entry.section}")
    lines.append(f"   URL：{entry.url}")
    if entry.description:
        lines.append(f"   说明：{entry.description[:300]}")
    return "\n".join(lines)


async def _search_knowledge(query: str) -> str:
    query = str(query or "").strip()
    if not query:
        return "检索问题为空，请提供更具体的关键词。"

    try:
        index = await _get_index()
    except KnowledgeError as e:
        return (
            f"知识库索引获取失败：{e}。"
            "请告知用户暂时无法查询知识库，并建议询问服主。"
        )

    if not index.entries:
        return "知识库索引为空，请告知用户暂时无法查询知识库，并建议询问服主。"

    matches = _search_entries(
        index.entries, query, config.AI_KNOWLEDGE_TOP_K
    )
    if not matches:
        available = "\n".join(
            f"- {entry.title}：{entry.url}"
            for entry in index.entries[:10]
        )
        return (
            f"知识库索引中没有直接匹配「{query}」的条目。"
            f"可用条目（前 10 条）：\n{available}\n"
            "可以换个关键词再次检索，或直接 fetch 看起来相关的条目。"
        )

    body = "\n\n".join(
        _format_entry(i + 1, entry)
        for i, entry in enumerate(matches)
    )
    return (
        f"知识库索引匹配结果（来自 {index.url}）：\n{body}\n\n"
        "如需正文，请用 fetch_url 抓取上面的 URL。"
    )


async def _fetch_url(url: str) -> str:
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
        text = await _get_page(normalized)
    except KnowledgeError as e:
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

GET_CURRENT_TIME = Tool(
    name="get_current_time",
    description="获取当前日期、时间和星期。",
    parameters={"type": "object", "properties": {}},
    handler=_get_current_time,
)

SEARCH_KNOWLEDGE = Tool(
    name="search_knowledge",
    description=(
        "检索 Seatide 知识库的 llms.txt 索引，返回匹配文档的标题、URL 和说明。"
        "回答服务器规则、玩法、插件、指令、公告、历史等问题前应先调用本工具，"
        "拿到 URL 后再用 fetch_url 抓取正文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "要检索的关键词或完整问题",
            }
        },
        "required": ["query"],
    },
    handler=_search_knowledge,
)

FETCH_URL = Tool(
    name="fetch_url",
    description=(
        "抓取知识库页面正文。url 必须是 search_knowledge 返回的 llms.txt "
        "条目 URL；不允许抓取任意网址。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "search_knowledge 返回的文档 URL",
            }
        },
        "required": ["url"],
    },
    handler=_fetch_url,
)

# 普通聊天 agent 可以查实时状态和时间
CHAT_TOOLS: tuple[Tool, ...] = (GET_SERVER_STATUS, GET_CURRENT_TIME)

# 知识库问答 agent：检索 llms.txt → fetch 正文，也可以查实时状态
KNOWLEDGE_TOOLS: tuple[Tool, ...] = (
    SEARCH_KNOWLEDGE,
    FETCH_URL,
    GET_SERVER_STATUS,
)