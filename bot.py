import os
from pathlib import Path

import nonebot
from nonebot.adapters.qq import Adapter as QQAdapter

# 运行环境：SEATIDE_ENV=dev（默认）或 prod，对应 .env.dev / .env.prod
ENV_NAME = os.getenv("SEATIDE_ENV", "dev").strip().lower()
_env_file = Path(__file__).resolve().parent / f".env.{ENV_NAME}"

nonebot.init(_env_file=str(_env_file) if _env_file.exists() else None)
nonebot.logger.info(f"运行环境: {ENV_NAME} ({_env_file.name})")

driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)

nonebot.load_plugin("src.plugins.mc_client")
nonebot.load_plugin("src.plugins.ai_chat")
nonebot.load_plugin("src.plugins.http_api")

if __name__ == "__main__":
    nonebot.run()
