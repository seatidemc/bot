# Seatide Bot

Seatide Minecraft 服务器的 QQ 群机器人，基于 [NoneBot2](https://nonebot.dev/) 和 QQ 官方机器人 API（WebSocket 客户端模式），接入 DeepSeek 做多轮对话，并对外提供一个发送群消息的 HTTP 接口。

机器人默认只服务一个群。

## 功能

- 群内命令：服务器状态、触发开机、查看自动归档倒计时、查看账号信息
- 私聊 `/登录` 换取后端 JWT，绑定 QQ 与网页账号
- 服务器上线/离线、实例状态变更自动推送到群（SSE）
- DeepSeek AI 对话：私聊任意文字、群里 @bot 任意文字
- HTTP 接口：后端或脚本调用后在群内发消息
- 两套配置：`.env.dev`（开发）与 `.env.prod`（生产）

## 项目结构

```
bot.py                          # 入口，选择环境并加载插件
src/plugins/mc_client/          # QQ 命令、SSE 状态推送、SQLite 持久化
src/plugins/ai_chat/            # DeepSeek 多轮对话
src/plugins/http_api/           # 对外 HTTP 接口
.env.example                    # 配置模板
.env.dev / .env.prod            # 实际配置（已 gitignore）
start.sh                        # Linux 下 screen 启动脚本
run_dev.bat / run_prod.bat      # Windows 启动脚本
data/                           # SQLite 与群 openid（已 gitignore）
AGENTS.md                       # 面向开发者的实现细节
```

## 环境要求

- Python 3.11+
- QQ 开放平台机器人（AppID + AppSecret）
- 可选：DeepSeek API Key
- 后端 `go-aliyunmc-v2` 运行在 `BACKEND_URL`（默认 `http://127.0.0.1:45678`）

## 快速开始

### Windows

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
copy .env.example .env.dev
copy .env.example .env.prod
```

填好两份 `.env` 中的 QQ 凭据和 DeepSeek Key 后，双击 `run_dev.bat` 或 `run_prod.bat`。

也可以在命令行启动：

```bat
set SEATIDE_ENV=dev
.venv\Scripts\python.exe bot.py
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env.dev
cp .env.example .env.prod
mkdir -p data
SEATIDE_ENV=dev python bot.py
```

生产环境可以用 `start.sh`（screen 管理）：

```bash
export SEATIDE_ENV=prod
./start.sh
```

`start.sh` 默认从 `~/bot` 启动，如部署路径不同请修改脚本里的 `BOT_DIR`。

## 配置

### 配置文件与运行环境

`bot.py` 根据环境变量 `SEATIDE_ENV` 选择配置：

| 值 | 加载文件 | 用途 |
|---|---|---|
| `dev`（默认） | `.env.dev` | 本地开发，DEBUG 日志 |
| `prod` | `.env.prod` | 生产环境，INFO 日志 |

两份配置都已加入 `.gitignore`，不会被提交。首次部署从 `.env.example` 复制后填写。

PowerShell 设置环境变量：

```powershell
$env:SEATIDE_ENV = "prod"
```

### QQ 机器人凭据

1. 登录 [QQ 开放平台](https://q.qq.com/)，完成主体认证
2. 创建机器人，进入「开发 → 开发设置」
3. 复制 **AppID** 和 **AppSecret**
4. 开通事件订阅：群聊 @ 消息、私聊消息
5. 把凭据填入 `QQ_BOTS`：

```env
QQ_BOTS='[
  {
    "id": "你的AppID",
    "secret": "你的AppSecret",
    "intent": {
      "c2c_group_at_messages": true
    }
  }
]'
```

说明：

- `id` 是字符串，即使 AppID 是纯数字也要加引号
- `secret` 对应 AppSecret，切勿泄露
- `token` 字段在当前 `nonebot-adapter-qq 1.7.4` 中不会被使用，可以省略
- 群里必须 @bot 才会收到消息
- 未上线的机器人只能在沙箱测试群使用，测试群人数上限 20

### 主要配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DRIVER` | `~fastapi+~httpx+~websockets` | 必填，HTTP 服务 + QQ WebSocket |
| `ENVIRONMENT` | `dev` | 由配置文件设置 |
| `LOG_LEVEL` | `DEBUG` / `INFO` | 日志级别 |
| `BACKEND_URL` | `http://127.0.0.1:45678` | 后端地址 |
| `BOT_KEY` | 无 | 与后端 `bot_key` 一致 |
| `BOT_USERNAME` | 无 | bot 自身的后端账号 |
| `BOT_PASSWORD` | 无 | 纯数字密码要加引号 |
| `GROUP_OPENID` | 空 | 目标群 openid，留空则自动记录 |
| `QQ_BOTS` | 无 | 机器人凭据 JSON 数组 |
| `DEEPSEEK_API_KEY` | 空 | 留空则关闭 AI 对话 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `AI_MAX_TURNS` | `10` | 每个用户保留的对话轮数 |
| `AI_HISTORY_TTL` | `3600` | 上下文空闲过期秒数 |
| `AI_MAX_REPLY_CHARS` | `1200` | 回复最大字符数，超出截断 |
| `HOST` / `PORT` | `127.0.0.1:8081` | HTTP 服务监听地址 |
| `HTTP_API_TOKEN` | 无 | 调用接口的鉴权 token |
| `HTTP_API_MAX_CHARS` | `2000` | 接口单条消息最大字符数 |

群 openid 不需要手动获取：机器人收到第一条群消息后会打印并写入 `data/group_openid.txt`，重启后继续生效。

## 命令

| 命令 | 别名 | 范围 | 说明 |
|---|---|---|---|
| `/登录 用户名 密码` | `login` | 私聊 | 换取后端 JWT，有效期 3 个月 |
| `/状态` | `status`、`zt` | 群/私聊 | 活跃实例、服务器在线状态与人数 |
| `/开机` | `start`、`kj` | 群/私聊 | 触发 `start_server` 任务 |
| `/释放时间` | `idle`、`sfsj`、`剩余时间` | 群/私聊 | 自动归档倒计时 |
| `/我的信息` | `info`、`wdxx`、`profile` | 群/私聊 | 用户名、角色、白名单绑定 |
| `/重置对话` | `reset`、`清空对话`、`clear` | 群/私聊 | 清空 AI 上下文 |

群内命令需要 @bot。未登录用户执行需要鉴权的命令时，bot 会直接在当前会话回复提醒。

## AI 对话

- 私聊直接发任意非命令文字
- 群内 @bot 后发任意文字
- 每次请求会带上该用户最近 `AI_MAX_TURNS` 轮上下文，空闲超过 `AI_HISTORY_TTL` 秒自动清空
- 上下文只保存在进程内存，重启后丢失
- `/重置对话` 可手动清空
- 未配置 `DEEPSEEK_API_KEY` 时 AI 功能关闭，私聊会收到命令帮助
- 同一个用户的消息串行处理，全局并发受 `AI_MAX_CONCURRENCY` 限制

## HTTP 接口

服务默认只监听 `127.0.0.1:8081`，随机器人一起启动。

### 发送群消息

```bash
curl -X POST http://127.0.0.1:8081/bot/group-message \
  -H "Content-Type: application/json" \
  -H "X-API-Token: <HTTP_API_TOKEN>" \
  -d '{"message": "服务器将在 10 分钟后重启"}'
```

也支持 `Authorization: Bearer <HTTP_API_TOKEN>`。

成功响应：

```json
{"ok": true, "group_openid": "xxx", "message_id": "xxx"}
```

错误响应：

| 状态码 | 含义 |
|---|---|
| 400 | message 为空或超过 `HTTP_API_MAX_CHARS` |
| 401 | token 缺失或错误 |
| 502 | QQ 接口发送失败（无权限、频控等） |
| 503 | token 未配置、机器人未连接、群 openid 未知 |

### 探活

```bash
curl http://127.0.0.1:8081/bot/health
# {"ok": true, "bot_ready": true, "group_configured": true}
```

该接口不校验 token，只返回布尔状态。

### 注意

- 群 openid 未知时先让群里 @bot 发一条消息，或手动填写 `GROUP_OPENID`
- QQ 对主动消息有频控和权限要求，无权限时接口返回 502
- 主动消息配额以开放平台后台为准；被动回复为 5 分钟内最多 5 条
- 如果调用方在其他机器，需要把 `HOST` 改为 `0.0.0.0` 并做好防火墙与 token 管理

## 状态推送

机器人用自身后端账号获取 JWT，后台连接两个 SSE 流：

- `/state/watch/server-status`：服务器上线/离线推送 🟢/🔴
- `/state/watch/instance-status`：实例状态变更推送 📦

需要目标群 openid 可用（`GROUP_OPENID` 或已自动记录）。连接断开后 5 秒重连，bot 自身 JWT 过期前 1 天自动续期。

## 数据与持久化

| 路径 | 内容 |
|---|---|
| `data/bot.db` | 用户 JWT、用户名、白名单 UUID |
| `data/group_openid.txt` | 自动记录的群 openid |

`data/`、`.env.dev`、`.env.prod`、`.venv/` 均在 `.gitignore` 中。

## 部署

- 开发机可直接双击 `run_dev.bat` / `run_prod.bat`
- Linux 服务器建议用 `start.sh` 配合 screen：

```bash
SEATIDE_ENV=prod ./start.sh
```

- 也可以交给 systemd，关键点是工作目录、虚拟环境里的 `python`、以及 `SEATIDE_ENV=prod`
- 部署前确认 `.env.prod` 中 `BOT_KEY` 与后端 `configs/main.toml` 的 `bot_key` 一致

## 常见问题

**群里 @bot 没反应**

检查机器人凭据、intent 是否为 `c2c_group_at_messages`、群里是否真的 @ 了机器人，以及群主是否为机器人开启了消息权限。

**启动报 `ModuleNotFoundError: No module named 'nonebot'`**

说明用到了系统 Python。使用 `run_dev.bat` / `run_prod.bat`，或显式运行 `.venv\Scripts\python.exe bot.py`。

**pip 找不到 `nonebot-adapter-qq`**

默认镜像可能缺包，改用清华源：

```bash
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

**接口返回 502**

多为 QQ 主动消息无权限或频控。到开放平台确认消息权限与配额，或改用被动回复。

**端口 8081 被占用**

修改 `.env` 中的 `PORT`，并同步修改调用方地址。

**机器人连不上 QQ**

确认 `id` / `secret` 正确、机器人已发布或所在群在沙箱测试范围内，并检查开放平台的 IP 白名单。

## 安全

- `.env.dev`、`.env.prod` 含 API Key 与机器人密钥，已被 gitignore，不要提交
- `HTTP_API_TOKEN` 等同于群消息发送权限，泄露后立即更换
- HTTP 服务默认只监听本机；暴露到公网前务必配置防火墙
- AppSecret 泄露可在 QQ 开放平台重置

## 开发

更细的实现说明见 `AGENTS.md`。

```bash
# 语法检查
.venv\Scripts\python.exe -m compileall -q bot.py src/plugins

# 离线加载插件（不连接 QQ）
.venv\Scripts\python.exe -c "import nonebot; nonebot.init(_env_file='.env.dev'); from nonebot.adapters.qq import Adapter; nonebot.get_driver().register_adapter(Adapter); [nonebot.load_plugin(n) for n in ('src.plugins.mc_client','src.plugins.ai_chat','src.plugins.http_api')]; print('loaded')"
```
