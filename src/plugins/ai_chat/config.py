"""DeepSeek 对话配置（由 .env.dev / .env.prod 提供）。"""

from pathlib import Path

from nonebot import get_driver
from nonebot.log import logger

_cfg = get_driver().config

# 项目根目录（bot.py 所在目录），相对路径的提示词文件以此为准
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_system_prompt() -> str:
    """从 AI_SYSTEM_PROMPT_FILE 指定的 Markdown 文件读取系统提示词。

    未配置或文件不可读/为空时返回空字符串，调用方会忽略系统提示词。
    """
    raw = str(getattr(_cfg, "ai_system_prompt_file", "") or "").strip()
    if not raw:
        return ""

    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _PROJECT_ROOT / path

    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"AI 系统提示词文件读取失败，已忽略: {path} ({e})")
        return ""

    if not content:
        logger.warning(f"AI 系统提示词文件为空，已忽略: {path}")
    return content


DEEPSEEK_API_KEY: str = str(getattr(_cfg, "deepseek_api_key", "") or "")
DEEPSEEK_BASE_URL: str = str(
    getattr(_cfg, "deepseek_base_url", "https://api.deepseek.com")
    or "https://api.deepseek.com"
).rstrip("/")
DEEPSEEK_MODEL: str = str(
    getattr(_cfg, "deepseek_model", "deepseek-chat") or "deepseek-chat"
)
# 系统提示词文件路径，留空、文件不存在或为空时不发送系统提示词
AI_SYSTEM_PROMPT: str = _load_system_prompt()
AI_TEMPERATURE: float = float(getattr(_cfg, "ai_temperature", 1.0))
# 单次回复的最大输出 token 数（含思考内容）
AI_MAX_TOKENS: int = max(1, int(getattr(_cfg, "ai_max_tokens", 800)))

# 思考强度：none 关闭思考，low / high / max 开启思考（DeepSeek reasoning_effort）
# 兼容别名：minimal → low，medium / xhigh → high
_THINKING_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}
_THINKING_EFFORTS = {"none", "low", "high", "max"}


def _load_thinking_effort() -> str:
    raw = str(getattr(_cfg, "ai_thinking_effort", "low") or "").strip().lower()
    raw = _THINKING_EFFORT_ALIASES.get(raw, raw)
    if raw not in _THINKING_EFFORTS:
        logger.warning(
            f"AI_THINKING_EFFORT 取值无效: {raw!r}，回退为 low（可选 none/low/high/max）"
        )
        return "low"
    return raw


AI_THINKING_EFFORT: str = _load_thinking_effort()
AI_TIMEOUT: float = float(getattr(_cfg, "ai_timeout", 60.0))
AI_MAX_TURNS: int = int(getattr(_cfg, "ai_max_turns", 10))
AI_HISTORY_TTL: int = int(getattr(_cfg, "ai_history_ttl", 3600))
AI_MAX_INPUT_CHARS: int = int(getattr(_cfg, "ai_max_input_chars", 2000))
AI_MAX_REPLY_CHARS: int = int(getattr(_cfg, "ai_max_reply_chars", 1200))
AI_MAX_CONCURRENCY: int = int(getattr(_cfg, "ai_max_concurrency", 4))


def _as_bool(value: object) -> bool:
    """环境变量里的布尔值可能以字符串形式出现。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# 群内不 @ 也参与聊天，回复走被动消息
AI_GROUP_CHAT_WITHOUT_AT: bool = _as_bool(
    getattr(_cfg, "ai_group_chat_without_at", False)
)
# 每分钟最多自动聊天回复次数（只统计未 @ 的群消息），0 表示关闭
AI_GROUP_CHAT_PER_MINUTE: int = int(
    getattr(_cfg, "ai_group_chat_per_minute", 5)
)

# 没有 API Key 时关闭 AI 对话，退回 mc_client 的命令提示
AI_ENABLED: bool = bool(DEEPSEEK_API_KEY)
