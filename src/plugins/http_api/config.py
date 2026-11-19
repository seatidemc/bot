"""对外 HTTP 接口配置。"""

from nonebot import get_driver

_cfg = get_driver().config

HTTP_API_TOKEN: str = str(getattr(_cfg, "http_api_token", "") or "")
HTTP_API_MAX_CHARS: int = int(getattr(_cfg, "http_api_max_chars", 2000))
