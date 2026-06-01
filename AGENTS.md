# AGENTS.md

Seatide MC QQ 机器人开发上下文。NoneBot2 + nonebot-adapter-qq（QQ 官方 API），群聊 @ 命令 + SSE 状态推送。

## 运行 / 部署

```bash
# 本地开发（macOS / Linux 均可直接运行，无需额外 QQ 客户端）
cd ~/code/seatide2026/bot
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
mkdir -p data          # SQLite 目录，aiosqlite 不会自动建
python bot.py
```

无需 Docker，无需 NapCat，无需 QQ 客户端。Bot 通过 WebSocket 直连 QQ 开放平台。

## 架构

```
QQ 用户 ──► QQ 开放平台 ──WS──► NoneBot2（bot.py，WS 客户端模式）
                                    │
                                    ├─ /登录、/login → POST /user/bot-token → 后端
                                    ├─ /状态、/status → GET /state/snapshot/*   → 后端
                                    ├─ /开机、/start → POST /task/trigger      → 后端
                                    ├─ /释放时间、/idle → GET /monitor/*        → 后端
                                    ├─ /我的信息、/info → GET /user/profile   → 后端
                                    └─ SSE 监听（bot 自身账号）→ 群推送
```

后端 `go-aliyunmc-v2` 运行在同一台机器的 `127.0.0.1:45678`。鉴权机制：

- **用户请求**：`X-Bot-Key` 头 + `Authorization: Bearer <jwt>`。JWT 由 `/user/bot-token` 签发，有效期 3 个月。`X-Bot-Key` 隔离网页端与 bot 端，外部拿到 JWT 也过不了中间件。
- **bot 自身请求**：同上，但使用 bot 的独立后端账号（operator 权限），用于 SSE 状态监听。
- 后端 `mid/auth.go` 检测到 `X-Bot-Key` 时走 JWT 验证，否则走原有 session cookie。

## 模块

| 文件 | 职责 |
|---|---|
| `bot.py` | NoneBot2 入口，注册 QQ 适配器，用 `load_plugin("src.plugins.mc_client")` 加载插件 |
| `src/plugins/mc_client/__init__.py` | 命令匹配器 + SSE 后台任务 + 启动/关闭生命周期 |
| `src/plugins/mc_client/config.py` | 从 NoneBot 配置读取环境变量。`BOT_PASSWORD` 强制 `str()` 转换以防 pydantic 把纯数字当 int |
| `src/plugins/mc_client/api.py` | 对后端 API 的 HTTP 封装。自动拼接 `X-Bot-Key` + `Bearer`，后端响应格式 `{"data": ...}` / `{"details": "..."}` 均正确处理。`sse_watch()` 按标准 SSE 解析 `event:` / `data:` 行 |
| `src/plugins/mc_client/db.py` | aiosqlite 持久化 `user_tokens` 表（`user_openid → jwt_token, username, whitelist_uuid, jwt_exp_ts`） |
| `test_mock_client.py` | [已废弃] 旧版 OneBot v11 模拟客户端，迁移至 adapter-qq 后不再可用 |

## 配置

所有配置通过 `.env` 文件，由 NoneBot 自动加载。关键变量：

| 变量 | 说明 |
|---|---|
| `DRIVER` | **必填** `~httpx+~websockets`。WS 客户端模式直连 QQ 开放平台 |
| `BACKEND_URL` | 后端地址。本机用 `http://127.0.0.1:45678` |
| `BOT_KEY` | 与后端 `configs/main.toml` 的 `bot_key` 一致 |
| `BOT_USERNAME` | bot 自身的后端账号（operator 权限，手动 `create_user` 创建） |
| `BOT_PASSWORD` | 纯数字密码须加引号 `"123"`，否则 pydantic 解析为 int 导致后端 JSON 反序列化失败 |
| `GROUP_OPENID` | 目标群 openid。首次启动后在群里 @bot，从日志获取填入 |
| `QQ_BOTS` | JSON 数组，含 bot 的 `id`、`token`、`secret`、`intent` |

## 命令

所有命令通过 `on_command` 匹配，支持中文主名 + 英文别名：

| 命令 | 别名 | 权限 | 说明 |
|---|---|---|---|
| `/登录 用户名 密码` | `login` | 仅私聊 | 调 `/user/bot-token` 获取 JWT，存入 SQLite，3 月有效 |
| `/状态` | `status`, `zt` | 仅群聊 | 活跃实例信息 + 服务器在线状态 + 玩家数 |
| `/开机` | `start`, `kj` | 仅群聊 | 触发 `start_server` 任务（要求用户已绑定白名单） |
| `/释放时间` | `idle`, `sfsj`, `剩余时间` | 仅群聊 | 自动归档倒计时，-1 表示无活动倒计时 |
| `/我的信息` | `info`, `wdxx`, `profile` | 仅群聊 | 用户名、角色、白名单绑定状态 |

未登录用户在群里发命令时，bot **直接回复**提醒消息到当前群。

## SSE 状态推送

bot 启动时用自身账号获取 JWT，后台连接两个 SSE 流：

- `/state/watch/server-status`：服务器上线/离线时群内推送 🟢/🔴
- `/state/watch/instance-status`：实例状态变更时推送 📦

bot 自身 JWT 过期前 1 天自动续期。SSE 连接断开自动重连（5 秒间隔）。snapshot 事件（首次连接时的当前快照）不推送，仅 update 事件触发消息。

## 本地测试

`test_mock_client.py` 模拟 NapCat 连接 bot 的 OneBot WS 端点，发送伪造的 QQ 消息。使用前提：

1. 后端运行中：`cd ~/code/go-aliyunmc-v2 && go run . run`
2. Bot 运行中：`cd ~/code/seatide2026/bot && source .venv/bin/activate && python bot.py`
3. 后端有一个可登录的用户（如 `testuser / testpass`）

```bash
source .venv/bin/activate && python test_mock_client.py
```

## 后端改动依赖

bot 依赖后端以下改动（已在 `~/code/go-aliyunmc-v2` 中实施）：

1. `mid/auth.go` — `X-Bot-Key` + JWT 鉴权通道，`InitBotAuth()` / `IsBotRequest()`
2. `routes/user_routes/handle_bot_token.go` — `POST /user/bot-token` 端点（无需 session）
3. `routes/user_routes/config.go` — `BotTokenConfig`（Secret + ExpireSeconds）
4. `configs/user-routes.toml` — `[bot_token]` 节
5. `config.go` + `configs/main.toml` — `BotKey` 字段
6. `main.go` — `mid.InitBotAuth(C.BotKey, user_routes.C.BotToken.Secret)` 调用

部署前确认 `bot_key` 和 `[bot_token].secret` 两端一致。

## 约定与踩坑

- `load_plugin("src.plugins.mc_client")` 指定模块名加载；`load_plugins("src.plugins")` 目录扫描无法发现子包
- 必须 `DRIVER=~httpx+~websockets`；缺少 websockets 则无法建立 WS 连接
- `data/` 目录需手动 `mkdir`，aiosqlite 不会自动创建
- `.env` 中纯数字值（如密码 `123`）会被 pydantic 解析为 int，config.py 强制 `str()` 保底
- 后端 Go 的 State 结构体字段大写开头（`Value`、`Error`、`UpdatedAt`），Python 侧取值注意大小写
- 后端错误响应字段名是 `details` 不是 `error`
- QQ 官方 API 用 `openid` 标识用户和群，不是 QQ 号
- `QQ_BOTS` 的 intent 只开了 `c2c_group_at_messages`，bot 只能收到私聊和群 @ 消息
- `GROUP_OPENID` 首次为空是正常的：启动后在群里 @bot，从日志复制 openid 填入后重启即可
- `.env`、`data/`、`.venv/` 均被 `.gitignore` 排除
- 项目无 CI/CD，服务器上直接 `python bot.py` 配合 systemd 或 screen/tmux 运行
