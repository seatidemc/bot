# AGENTS.md

Seatide MC QQ 机器人开发上下文。NoneBot2 + nonebot-adapter-qq（QQ 官方 API），群聊 @ 命令。

## 运行 / 部署

```bash
# 本地开发（macOS / Linux 均可直接运行，无需额外 QQ 客户端）
cd ~/code/seatide2026/bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
mkdir -p data          # 群 openid 等运行数据目录
SEATIDE_ENV=dev python bot.py
```

无需 Docker，无需 NapCat，无需 QQ 客户端。Bot 通过 WebSocket 直连 QQ 开放平台。

## 架构

```
QQ 用户 ──► QQ 开放平台 ──WS──► NoneBot2（bot.py，WS 客户端模式）
                                    │
                                    ├─ /状态、/status → GET  /bot-v1/server-status            → 后端
                                    └─ /启动、/start → POST /bot-v1/trigger-one-click-start  → 后端
```

后端 `go-aliyunmc-v2` 运行在同一台机器的 `127.0.0.1:45678`，bot 只通过 `BACKEND_BASE_URL` 连接。`/bot-v1` 只接受本机回环直连，**不需要 token、签名或用户登录态**，所以 bot 侧没有登录流程，也不存用户凭据。

## 模块

| 文件 | 职责 |
|---|---|
| `bot.py` | NoneBot2 入口，注册 QQ 适配器，用 `load_plugin("src.plugins.lab_client")` 加载插件 |
| `src/plugins/lab_client/__init__.py` | 命令匹配器、群 openid 自动发现、状态格式化 |
| `src/plugins/lab_client/config.py` | 从 NoneBot 配置读取 `BACKEND_BASE_URL`、`GROUP_OPENID` |
| `src/plugins/lab_client/api.py` | 对后端 `/bot-v1` 的 HTTP 封装（`ping` / `server-status` / `trigger-one-click-start`）。无鉴权，后端响应格式 `{"data": ...}` / `{"details": "..."}` |
| `src/plugins/ai_chat/` | DeepSeek 多轮对话插件：`config.py` 读配置，`api.py` 调 `/chat/completions`，`memory.py` 进程内保存每用户最近 N 轮上下文，`__init__.py` 注册私聊 / 群 @ 聊天 |
| `src/plugins/http_api/` | 对外 HTTP 接口：`POST /bot/group-message` 向固定群发消息，`GET /bot/health` 探活；仅回环地址，随 NoneBot 的 FastAPI 驱动启动 |
| `test_mock_client.py` | [已废弃] 旧版 OneBot v11 模拟客户端，迁移至 adapter-qq 后不再可用 |

## 配置

所有配置通过 `.env` 文件，由 NoneBot 自动加载。关键变量：

| 变量 | 说明 |
|---|---|
| `DRIVER` | **必填** `~httpx+~websockets`。WS 客户端模式直连 QQ 开放平台 |
| `BACKEND_BASE_URL` | 后端地址，唯一需要配置的后端参数。本机用 `http://127.0.0.1:45678` |
| `GROUP_OPENID` | 目标群 openid。首次收到群消息会自动记录并写入 `data/group_openid.txt`，重启后继续生效；也可手动填入固定使用 |
| `QQ_BOTS` | JSON 数组，含 bot 的 `id`、`token`、`secret`、`intent` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key，留空则 AI 对话关闭（私聊自动回复命令帮助） |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 默认 `deepseek-chat` |
| `AI_SYSTEM_PROMPT_FILE` | 系统提示词 Markdown 路径（如 `prompts/system_prompt.md`）；留空或路径无效则不发送系统提示词 |
| `AI_MAX_TOKENS` | 单次回复最大输出 token 数（含思考内容），默认 800 |
| `AI_THINKING_EFFORT` | 思考强度，默认 `low`；`none` 关闭思考，`low` / `high` / `max` 开启思考 |
| `AI_MAX_TURNS` | 每用户保留的对话轮数，默认 10 |
| `AI_HISTORY_TTL` | 上下文空闲过期秒数，默认 3600 |
| `AI_GROUP_CHAT_WITHOUT_AT` | 群内不 @ 是否参与聊天，默认 false |
| `AI_GROUP_CHAT_PER_MINUTE` | 每个群每分钟自动聊天回复上限，默认 5 |
| `HOST` / `PORT` | HTTP 服务监听地址，默认 `127.0.0.1:8081`，建议只监听本机 |
| `HTTP_API_MAX_CHARS` | 单条消息最大字符数，默认 2000 |

## 命令

所有命令通过 `on_command` 匹配，支持中文主名 + 英文别名：

| 命令 | 别名 | 权限 | 说明 |
|---|---|---|---|
| `/状态` | `status`, `zt` | 群/私聊 | 调 `GET /bot-v1/server-status`：在线状态、IP、玩家数、运行时长、内存/CPU、预计释放时间 |
| `/启动` | `start`, `qd` | 群/私聊 | 调 `POST /bot-v1/trigger-one-click-start`；任务已在跑时提示无需重复触发 |

命令不需要登录；后端按来源地址限流，`/启动` 遇到 `409`（其他部署链任务执行中）或 `429` 会直接回复原因。

## 本地测试

`test_mock_client.py` 模拟 NapCat 连接 bot 的 OneBot WS 端点，发送伪造的 QQ 消息。使用前提：

1. 后端运行中：`cd ~/code/go-aliyunmc-v2 && go run . run`
2. Bot 运行中：`cd ~/code/seatide2026/bot && source .venv/bin/activate && python bot.py`
3. 后端已启用 `[bot] enabled = true`（`curl http://127.0.0.1:45678/bot-v1/ping` 返回 200）

```bash
source .venv/bin/activate && python test_mock_client.py
```

## 后端接入

- 后端 `go-aliyunmc-v2` 的机器人模块前缀为 `/bot-v1`（由后端 `bot.Bind` 注册），只接受本机回环地址的请求，无需 `X-Bot-Key`。
- 后端 `/bot-v1` 当前只有 `GET /ping`、`GET /server-status`、`POST /trigger-one-click-start`，接口细节见 `go-aliyunmc-v2/docs/bot-server-api.md`。
- bot 侧只配置 `BACKEND_BASE_URL` 一项后端参数；如部署时后端挂在路径前缀下，把前缀一并写进该变量即可。
- `/bot-v1` 不签发用户登录态，bot 不再保存用户名/密码/JWT，任何人都能触发 `/启动`，仅受后端 `rate_limit_per_minute` 限制。

## 约定与踩坑

- `load_plugin("src.plugins.lab_client")` 指定模块名加载；`load_plugins("src.plugins")` 目录扫描无法发现子包
- 必须 `DRIVER=~httpx+~websockets`；缺少 websockets 则无法建立 WS 连接
- `data/` 目录需手动 `mkdir`，`data/group_openid.txt` 依赖它存在
- `/bot-v1/server-status` 的字段是 camelCase（`uptimeSeconds`、`usagePercent`、`usedCores`、`release.inSeconds` 等），且 `uptimeSeconds`、`memory`、`cpu`、`players.max` 可能为 `null`，格式化前要判空
- 后端错误响应字段名是 `details` 不是 `error`
- QQ 官方 API 用 `openid` 标识用户和群，不是 QQ 号
- 群主开启「获取群内全部消息」后，事件类型从 `GROUP_AT_MESSAGE_CREATE` 变为 `GROUP_MESSAGE_CREATE`；插件统一用 `GroupMessageCreateEvent` 基类，并用 `event.to_me` / `mentions.is_you` 判断是否被 @
- `QQ_BOTS` 的 intent 只开了 `c2c_group_at_messages`，bot 只能收到私聊和群 @ 消息
- `GROUP_OPENID` 首次为空是正常的：启动后在群里 @bot，从日志复制 openid 填入后重启即可
- `.env`、`data/`、`.venv/` 均被 `.gitignore` 排除
- 项目无 CI/CD，服务器上直接 `python bot.py` 配合 systemd 或 screen/tmux 运行

## 多环境配置

配置文件拆分为两份，由 `bot.py` 根据 `SEATIDE_ENV` 选择加载（默认 `dev`）：

| 文件 | 用途 |
|---|---|
| `.env.dev` | 开发环境，`LOG_LEVEL=DEBUG` |
| `.env.prod` | 生产环境，`LOG_LEVEL=INFO` |

```bash
SEATIDE_ENV=dev  python bot.py   # 开发
SEATIDE_ENV=prod python bot.py   # 生产
```

`start.sh` 同样支持：`SEATIDE_ENV=prod ./start.sh`。两个文件均加入 `.gitignore`，避免 API Key 进入仓库。

## DeepSeek AI 对话

- 私聊任意非命令文本、或群里 @bot 发任意文本 → 调用 DeepSeek 回复
- 上下文保存在进程内存，无活动超过 `AI_HISTORY_TTL` 秒自动过期，重启丢失（不提供手动清空命令）
- 同一用户的消息串行处理，全局并发受 `AI_MAX_CONCURRENCY` 限制
- 开启 `AI_GROUP_CHAT_WITHOUT_AT` 后，未 @ 的群消息也会进入 AI；按群每分钟限流，回复走被动消息 `msg_id`，不使用主动消息额度
- 未配置 `DEEPSEEK_API_KEY` 时 AI 匹配器不启用，`lab_client` 的兜底提示继续生效
- 调用失败（超时 / 401 / 402 / 429）会直接把原因回复给用户
- 每次成功调用在 `api.py` 里以 INFO 记录 `DeepSeek 用量`（prompt / completion / 思考 token / 缓存命中），用于观察成本

## Windows 本地运行

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -e . -i https://pypi.tuna.tsinghua.edu.cn/simple
```

之后双击 `run_dev.bat` / `run_prod.bat`，或手动：

```bat
set SEATIDE_ENV=dev
.venv\Scripts\python.exe bot.py
```

PowerShell 用 `$env:SEATIDE_ENV="prod"`。默认 pip 源缺少 `nonebot-adapter-qq` 时，加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

## 对外 HTTP 接口

NoneBot 驱动改为 `~fastapi+~httpx+~websockets`，机器人在 QQ WebSocket 之外额外启动一个 HTTP 服务（默认 `127.0.0.1:8081`），供后端或脚本触发群消息。接口只接受本机回环地址的请求，无 token 鉴权。

### 发送群消息

```bash
curl -X POST http://127.0.0.1:8081/bot/group-message   -H "Content-Type: application/json"   -d '{"message": "服务器将在 10 分钟后重启"}'
```

成功返回：

```json
{"ok": true, "group_openid": "xxx", "message_id": "xxx"}
```

失败返回：

| 状态码 | 含义 |
|---|---|
| 400 | message 为空或超过 `HTTP_API_MAX_CHARS` |
| 403 | 非本机请求，或携带了转发头 |
| 502 | QQ 接口发送失败（权限、频率限制等） |
| 503 | 机器人未连接、群 openid 未知 |

### 探活

```bash
curl http://127.0.0.1:8081/bot/health
# {"ok": true, "bot_ready": true, "group_configured": true}
```

### 注意

- 目标群固定为 `GROUP_OPENID`，未配置时使用首次收到群消息自动记录的 openid，并持久化到 `data/group_openid.txt`。
- 服务默认只监听 `127.0.0.1`，不要随意改成 `0.0.0.0`；接口本身也会拒绝非回环来源和带 `Forwarded` / `X-Forwarded-For` / `X-Real-IP` 的请求。
- QQ 官方接口对主动消息有频率和权限限制（被动回复 5 分钟内 5 条、主动推送需平台开通能力）。若返回 502，先查开放平台的消息权限与配额。
