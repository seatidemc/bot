"""DeepSeek 对话配置（由 .env.dev / .env.prod 提供）。"""

from nonebot import get_driver

_cfg = get_driver().config

_DEFAULT_SYSTEM_PROMPT = (
    "你是 Seatide Minecraft 服务器的 QQ 群助手，名字叫海潮。"
    "请用简洁、友好的中文回答，默认控制在 200 字以内。"
    "群内可用命令：/登录 用户名 密码（需私聊）、/状态、/开机、/释放时间、"
    "/我的信息、/重置对话。"
    "如果被问到与服务器无关的问题也可以正常闲聊，但不要编造服务器状态，"
    "实时状态请引导用户使用 /状态 命令查询。"
)

DEEPSEEK_API_KEY: str = str(getattr(_cfg, "deepseek_api_key", "") or "")
DEEPSEEK_BASE_URL: str = str(
    getattr(_cfg, "deepseek_base_url", "https://api.deepseek.com")
    or "https://api.deepseek.com"
).rstrip("/")
DEEPSEEK_MODEL: str = str(
    getattr(_cfg, "deepseek_model", "deepseek-chat") or "deepseek-chat"
)
AI_SYSTEM_PROMPT: str = str(
    getattr(_cfg, "ai_system_prompt", "") or _DEFAULT_SYSTEM_PROMPT
)
AI_TEMPERATURE: float = float(getattr(_cfg, "ai_temperature", 1.0))
AI_MAX_TOKENS: int = int(getattr(_cfg, "ai_max_tokens", 800))
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
