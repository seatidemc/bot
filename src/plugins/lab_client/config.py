"""从 NoneBot 配置中读取 bot 所需的环境变量。"""

from nonebot import get_driver

_driver = get_driver()
_cfg = _driver.config

BACKEND_BASE_URL: str = getattr(_cfg, "backend_base_url", "http://127.0.0.1:45678").rstrip("/")
# QQ 官方 API 用 openid 标识群，非数字群号
GROUP_OPENID: str = getattr(_cfg, "group_openid", "")
