"""AI 工作流配置：router / chat / knowledge 三个模型分别可配。

每个模型都可以单独配置自己的 API Key；端点、模型 id、temperature、
thinking effort、最大输出 token、超时和系统提示词文件也可以分别配置，
未单独配置时回退到 ``AI_DEFAULT_*`` 默认值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from nonebot import get_driver
from nonebot.log import logger

_cfg = get_driver().config

# 项目根目录（bot.py 所在目录），相对路径的提示词文件以此为准
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ─── 读取工具 ────────────────────────────────────────────────


def _raw(name: str):
    return getattr(_cfg, name, None)


def _text(name: str, default: str = "") -> str:
    """读取字符串配置；空字符串视为未配置。"""
    value = _raw(name)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _float(name: str, default: float) -> float:
    value = _raw(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name.upper()} 取值无效: {value!r}") from None


def _int(name: str, default: int) -> int:
    value = _raw(name)
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name.upper()} 取值无效: {value!r}") from None


def _as_bool(value: object, default: bool = False) -> bool:
    """环境变量里的布尔值可能以字符串形式出现。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ─── 思考强度 ────────────────────────────────────────────────

# none 关闭思考，low / high / max 开启思考（DeepSeek reasoning_effort）
# 兼容别名：minimal → low，medium / xhigh → high
_THINKING_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}
_THINKING_EFFORTS = {"none", "low", "high", "max"}


def _thinking_effort(name: str, default: str, label: str) -> str:
    raw = _text(name).lower()
    if not raw:
        return default
    raw = _THINKING_EFFORT_ALIASES.get(raw, raw)
    if raw not in _THINKING_EFFORTS:
        raise ValueError(
            f"{label} 取值无效: {raw!r}（可选 none/low/high/max）"
        )
    return raw


def _load_prompt(file_value: str, component: str) -> str:
    """读取组件对应的系统提示词文件；失败时返回空字符串。

    空字符串会让启动校验失败，不会静默使用内置提示词。
    """
    raw = (file_value or "").strip()
    if not raw:
        logger.warning(f"[{component}] 未配置系统提示词文件")
        return ""

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path

    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(
            f"[{component}] 系统提示词文件读取失败: {path} ({e})"
        )
        return ""

    if not content:
        logger.warning(f"[{component}] 系统提示词文件为空: {path}")
    return content


# ─── 模型配置 ────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelConfig:
    """一个 OpenAI 兼容 Chat Completions 端点的完整配置。"""

    component: str
    api_key: str
    base_url: str
    model: str
    temperature: float
    max_tokens: int
    thinking_effort: str
    timeout: float
    system_prompt: str
    prompt_file: str


# AI_DEFAULT_* 是三个模型共用的 fallback 值
AI_DEFAULT_API_KEY: str = _text("ai_default_api_key")
AI_DEFAULT_BASE_URL: str = _text(
    "ai_default_base_url", "https://api.deepseek.com"
).rstrip("/")
AI_DEFAULT_MODEL: str = _text("ai_default_model", "deepseek-chat")
AI_DEFAULT_TEMPERATURE: float = _float("ai_default_temperature", 1.0)
AI_DEFAULT_MAX_TOKENS: int = _int("ai_default_max_tokens", 800)
AI_DEFAULT_TIMEOUT: float = _float("ai_default_timeout", 60.0)
AI_DEFAULT_THINKING_EFFORT = _thinking_effort(
    "ai_default_thinking_effort", "low", "AI_DEFAULT_THINKING_EFFORT"
)


def _build_chat() -> ModelConfig:
    prompt_file = _text(
        "ai_chat_system_prompt_file", "prompts/chat_prompt.md"
    )
    return ModelConfig(
        component="chat",
        api_key=_text("ai_chat_api_key") or AI_DEFAULT_API_KEY,
        base_url=(
            _text("ai_chat_base_url") or AI_DEFAULT_BASE_URL
        ).rstrip("/"),
        model=_text("ai_chat_model") or AI_DEFAULT_MODEL,
        temperature=_float(
            "ai_chat_temperature", AI_DEFAULT_TEMPERATURE
        ),
        max_tokens=_int("ai_chat_max_tokens", AI_DEFAULT_MAX_TOKENS),
        thinking_effort=_thinking_effort(
            "ai_chat_thinking_effort",
            AI_DEFAULT_THINKING_EFFORT,
            "AI_CHAT_THINKING_EFFORT",
        ),
        timeout=_float("ai_chat_timeout", AI_DEFAULT_TIMEOUT),
        system_prompt=_load_prompt(prompt_file, "chat"),
        prompt_file=prompt_file,
    )


CHAT: ModelConfig = _build_chat()

_ROUTER_PROMPT_FILE = _text(
    "ai_router_system_prompt_file", "prompts/router_prompt.md"
)
ROUTER: ModelConfig = ModelConfig(
    component="router",
    api_key=_text("ai_router_api_key") or AI_DEFAULT_API_KEY,
    base_url=(
        _text("ai_router_base_url") or AI_DEFAULT_BASE_URL
    ).rstrip("/"),
    model=_text("ai_router_model") or AI_DEFAULT_MODEL,
    temperature=_float(
        "ai_router_temperature", AI_DEFAULT_TEMPERATURE
    ),
    max_tokens=_int("ai_router_max_tokens", AI_DEFAULT_MAX_TOKENS),
    thinking_effort=_thinking_effort(
        "ai_router_thinking_effort",
        AI_DEFAULT_THINKING_EFFORT,
        "AI_ROUTER_THINKING_EFFORT",
    ),
    timeout=_float("ai_router_timeout", AI_DEFAULT_TIMEOUT),
    system_prompt=_load_prompt(_ROUTER_PROMPT_FILE, "router"),
    prompt_file=_ROUTER_PROMPT_FILE,
)

_KNOWLEDGE_PROMPT_FILE = _text(
    "ai_knowledge_system_prompt_file", "prompts/knowledge_prompt.md"
)
KNOWLEDGE: ModelConfig = ModelConfig(
    component="knowledge",
    api_key=_text("ai_knowledge_api_key") or AI_DEFAULT_API_KEY,
    base_url=(
        _text("ai_knowledge_base_url") or AI_DEFAULT_BASE_URL
    ).rstrip("/"),
    model=_text("ai_knowledge_model") or AI_DEFAULT_MODEL,
    temperature=_float(
        "ai_knowledge_temperature", AI_DEFAULT_TEMPERATURE
    ),
    max_tokens=_int(
        "ai_knowledge_max_tokens", AI_DEFAULT_MAX_TOKENS
    ),
    thinking_effort=_thinking_effort(
        "ai_knowledge_thinking_effort",
        AI_DEFAULT_THINKING_EFFORT,
        "AI_KNOWLEDGE_THINKING_EFFORT",
    ),
    timeout=_float("ai_knowledge_timeout", AI_DEFAULT_TIMEOUT),
    system_prompt=_load_prompt(_KNOWLEDGE_PROMPT_FILE, "knowledge"),
    prompt_file=_KNOWLEDGE_PROMPT_FILE,
)


# ─── 工作流 / 会话设置 ───────────────────────────────────────

# AI 总开关：整个 AI 链路只有这一个开关
AI_ENABLED: bool = _as_bool(_raw("ai_enabled"), True)

# 群聊上下文直接取自永久聊天记录：每次请求预置最近多少条
AI_CHAT_LOG_RECENT: int = min(200, max(0, _int("ai_chat_log_recent", 20)))

# router 参考最近多少轮对话（用于理解追问和指代）
AI_ROUTER_HISTORY_TURNS: int = max(0, _int("ai_router_history_turns", 3))

# 知识库：llms.txt 索引 + 按需 fetch
# llms.txt 是一个 Markdown 索引，通常形如：
#   # 知识库标题
#   > 简介
#   ## 章节
#   - [页面标题](https://example.com/doc.md): 页面说明
AI_KNOWLEDGE_LLMS_TXT_URL: str = _text("ai_knowledge_llms_txt_url")
# search_knowledge 最多返回多少条索引条目
AI_KNOWLEDGE_TOP_K: int = max(1, _int("ai_knowledge_top_k", 5))
AI_KNOWLEDGE_FETCH_TIMEOUT: float = max(
    1.0, _float("ai_knowledge_fetch_timeout", 15.0)
)
# 索引/页面内容的内存缓存秒数，0 表示不缓存
AI_KNOWLEDGE_CACHE_TTL: int = max(0, _int("ai_knowledge_cache_ttl", 300))
# fetch_url 允许抓取的 URL 正则（完整匹配）；为空表示只受 llms.txt 索引约束
AI_KNOWLEDGE_FETCH_URL_REGEX: str = _text("ai_knowledge_fetch_url_regex")

# 群内不 @ 也参与聊天，回复走被动消息
AI_GROUP_CHAT_WITHOUT_AT: bool = _as_bool(
    _raw("ai_group_chat_without_at"), False
)
# 每分钟最多自动聊天回复次数（只统计未 @ 的群消息），0 表示关闭
AI_GROUP_CHAT_PER_MINUTE: int = _int("ai_group_chat_per_minute", 5)


# ─── 启动校验 ────────────────────────────────────────────────


def _valid_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def validate_agent_configs() -> list[str]:
    """检查可静态验证的 agent 配置；返回错误列表，空列表表示通过。

    AI 开启时所有 agent 都参与链路，因此三个模型的 Key、端点、模型 id、
    系统提示词和 knowledge 的 llms.txt URL 都必须是可用配置，避免把
    配置错误拖到运行时才发现。
    """
    if not AI_ENABLED:
        return []

    problems: list[str] = []
    key_hints = {
        "chat": "AI_CHAT_API_KEY 或 AI_DEFAULT_API_KEY",
        "router": "AI_ROUTER_API_KEY 或 AI_DEFAULT_API_KEY",
        "knowledge": "AI_KNOWLEDGE_API_KEY 或 AI_DEFAULT_API_KEY",
    }
    for model in (ROUTER, CHAT, KNOWLEDGE):
        label = model.component
        if not model.api_key:
            problems.append(
                f"{label}: 未配置 API Key（{key_hints[label]}）"
            )
        if not model.model:
            problems.append(f"{label}: 模型 id 为空")
        if not _valid_http_url(model.base_url):
            problems.append(
                f"{label}: BASE_URL 必须是 http/https URL，"
                f"当前为 {model.base_url!r}"
            )
        if not model.system_prompt:
            problems.append(
                f"{label}: 系统提示词为空或读取失败"
                f"（{model.prompt_file}）"
            )
        if not (0.0 <= model.temperature <= 2.0):
            problems.append(
                f"{label}: TEMPERATURE 必须在 0~2 之间，"
                f"当前为 {model.temperature}"
            )
        if model.max_tokens <= 0:
            problems.append(
                f"{label}: MAX_TOKENS 必须大于 0，当前为 {model.max_tokens}"
            )
        if model.timeout <= 0:
            problems.append(
                f"{label}: TIMEOUT 必须大于 0，当前为 {model.timeout}"
            )

    if not AI_KNOWLEDGE_LLMS_TXT_URL:
        problems.append(
            "knowledge: 未配置 AI_KNOWLEDGE_LLMS_TXT_URL（llms.txt 索引 URL）"
        )
    elif not _valid_http_url(AI_KNOWLEDGE_LLMS_TXT_URL):
        problems.append(
            "knowledge: AI_KNOWLEDGE_LLMS_TXT_URL 必须是 http/https URL"
        )

    if AI_KNOWLEDGE_FETCH_URL_REGEX:
        try:
            re.compile(AI_KNOWLEDGE_FETCH_URL_REGEX)
        except re.error as e:
            problems.append(
                "knowledge: AI_KNOWLEDGE_FETCH_URL_REGEX "
                f"不是合法正则：{e}"
            )

    return problems