"""从 NoneBot 配置中读取 bot 所需的环境变量。"""

from nonebot import get_driver

_driver = get_driver()
_cfg = _driver.config

BACKEND_URL: str = getattr(_cfg, "backend_url", "http://127.0.0.1:45678")
BOT_KEY: str = getattr(_cfg, "bot_key", "")
BOT_USERNAME: str = getattr(_cfg, "bot_username", "")
BOT_PASSWORD: str = str(getattr(_cfg, "bot_password", ""))
GROUP_ID: str = str(getattr(_cfg, "group_id", ""))
