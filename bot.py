import nonebot
from nonebot.adapters.qq import Adapter as QQAdapter

nonebot.init()
driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)

nonebot.load_plugin("src.plugins.mc_client")

if __name__ == "__main__":
    nonebot.run()
