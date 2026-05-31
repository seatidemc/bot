# AGENTS.md

Seatide MC QQ 机器人开发上下文。NoneBot2 + NapCat，群聊命令 + SSE 状态推送。

## 运行 / 部署

```bash
# 本地开发
cd ~/code/seatide2026/bot
pip install -e .
python bot.py

# 生产部署
QQ_ACCOUNT=<bot的QQ号> docker compose up -d
# 然后访问 http://服务器IP:6099 完成 NapCat WebUI 扫码登录
```

NapCat 扫码一次后凭据持久化在 `napcat/` 目录，后续重启无需重新扫码。修改 `.env` 后需 `docker compose restart bot`。

## 架构

```
QQ 用户 ──► NapCat（QQ NT 桥）──WS──► NoneBot2（bot.py）
                                         │
                                         ├─ /登录 → POST /user/bot-token → 后端
                                         ├─ /状态 → GET /state/snapshot/*   → 后端
                                         ├─ /开机 → POST /task/trigger     → 后端
                                         ├─ /释放时间 → GET /monitor/*       → 后端
                                         ├─ /我的信息 → GET /user/profile  → 后端
                                         └─ SSE 监听（bot 自身账号）→ 群推送
```

后端 `go-aliyunmc-v2` 运行在同一台机器的 `127.0.0.1:45678`。鉴权机制：

- **用户请求**：`X-Bot-Key` 头 + `Authorization: Bearer <jwt>`。JWT 由 `/user/bot-token` 签发，有效期 3 个月。`X-Bot-Key` 隔离网页端与 bot 端。
- **bot 自身请求**：同上，但使用 bot 的独立后端账号，用于 SSE 状态监听。
- 后端 `mid/auth.go` 检测到 `X-Bot-Key` 时走 JWT 验证，否则走原有 session cookie。

## 模块

| 文件 | 职责 |
|---|---|
| `bot.py` | NoneBot2 入口，注册 OneBot 适配器，加载插件 |
| `src/plugins/mc_client/__init__.py` | 命令匹配器 + SSE 后台任务 + 启动/关闭生命周期 |
| `src/plugins/mc_client/config.py` | 从 NoneBot 配置读取环境变量（`.env`） |
| `src/plugins/mc_client/api.py` | 对后端 API 的 HTTP 封装。`_request()` 自动拼接 `X-Bot-Key` 和 `Bearer` 头，错误时抛 `APIError`。`sse_watch()` 按 SSE 标准解析 `event:` / `data:` 行 |
| `src/plugins/mc_client/db.py` | aiosqlite 持久化 `user_tokens` 表（`qq_number → jwt_token, username, whitelist_uuid, jwt_exp_ts`） |

## 配置

所有配置通过 `.env` 文件，由 NoneBot 自动加载。关键变量：

| 变量 | 说明 |
|---|---|
| `BACKEND_URL` | 后端地址。Docker 部署用 `http://host.docker.internal:45678`，本机用 `http://127.0.0.1:45678` |
| `BOT_KEY` | 与后端 `configs/main.toml` 的 `bot_key` 一致 |
| `BOT_USERNAME` / `BOT_PASSWORD` | bot 自身的后端账号（operator 权限，手动 `create_user` 创建） |
| `GROUP_ID` | 目标 QQ 群号 |
| `HOST` / `PORT` | NoneBot 监听的地址和端口（供 NapCat 反向 WS 连接） |

## 命令

所有命令通过 `on_command` 匹配（NoneBot 自动 strip 前缀 `/`）：

| 命令 | 权限 | 说明 |
|---|---|---|
| `/登录 用户名 密码` | 仅私聊 | 调 `/user/bot-token` 获取 JWT，3 月有效 |
| `/状态` | 仅群聊 | 实例状态 + 服务器在线状态 + 玩家数 |
| `/开机` | 仅群聊 | 触发 `start_server` 任务（需已绑定白名单） |
| `/释放时间` | 仅群聊 | 自动归档倒计时 |
| `/我的信息` | 仅群聊 | 用户名、角色、绑定状态 |

未登录用户发群命令时，bot 会私聊提醒（通过 `_send_group` 在群内 @）。

## SSE 状态推送

bot 启动时用自身账号获取 JWT，连接两个 SSE 流：

- `/state/watch/server-status`：服务器上线/离线时群内推送 🟢/🔴 
- `/state/watch/instance-status`：实例状态变更时推送 📦

SSE 连接断开自动重连（5 秒间隔）。snapshot 事件（首次连接）不推送，仅 update 事件触发消息。

## 后端改动依赖

bot 依赖后端以下改动（已在 `~/code/go-aliyunmc-v2` 中实施）：

1. `mid/auth.go` — `X-Bot-Key` + JWT 鉴权通道
2. `routes/user_routes/handle_bot_token.go` — `/user/bot-token` 端点
3. `routes/user_routes/config.go` — `BotTokenConfig`
4. `config.go` — `BotKey` 字段
5. `main.go` — `mid.InitBotAuth(...)` 调用

如果 bot 部署后发现 403/401，检查 `bot_key` 和 `[bot_token].secret` 是否两端一致，以及配置文件中 `[bot_token]` 节是否存在。

## 约定

- 数据库文件 `data/bot.db` 由 Docker volume 挂载到宿主机，不随容器销毁丢失
- `data/` 目录需手动创建（`mkdir -p data`），首次启动时 aiosqlite 不会自动建目录
- 用户 JWT 过期后 bot 返回友好提示引导重新 `/登录`，不做静默刷新
- 所有后端错误（401/403/404/500）通过 `APIError` 透传，命令处理器直接向用户展示 `e.message`
- `.env` 和 `data/` 和 `napcat/` 均被 `.gitignore` 排除
- 项目不设 CI/CD，手动 `docker compose` 管理
- 插件加载用 `load_plugin("src.plugins.mc_client")`（指定模块名），不用 `load_plugins("src.plugins")`（目录扫描，在本项目结构下无法发现子包）
- 必须用 `DRIVER=~fastapi`（服务端驱动），`~httpx` 是纯客户端驱动不支持反向 WS
