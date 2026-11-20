# Seatide Bot

Seatide Minecraft 服务器的 QQ 群机器人，基于 [NoneBot2](https://nonebot.dev/) 和 QQ 官方机器人 API（WebSocket 客户端模式），接入 DeepSeek 做多轮对话，并对外提供一个发送群消息的 HTTP 接口。

机器人默认只服务一个群。

## 功能

- 群内命令：服务器状态（在线、人数、资源占用、预计释放时间）、一键启动服务器
- DeepSeek AI 对话：私聊任意文字，群内 @bot 或开启自动聊天后直接发文字
- 群内自动聊天支持每分钟限流，回复走被动消息，不占用主动消息额度
- 支持群消息全量模式（GROUP_MESSAGE_CREATE），只响应 @、回复或自动聊天
- HTTP 接口：后端或脚本调用后在群内发消息
- 两套配置：`.env.dev`（开发）与 `.env.prod`（生产）

## 项目结构

```
bot.py                          # 入口，选择环境并加载插件
src/plugins/lab_client/         # QQ 命令（对接后端 /bot-v1）
src/plugins/ai_chat/            # DeepSeek 多轮对话
src/plugins/http_api/           # 对外 HTTP 接口
.env.example                    # 配置模板
.env.dev / .env.prod            # 实际配置（已 gitignore）
start.sh                        # Linux / macOS 一键启动（dev 前台 / prod screen）
run_dev.bat / run_prod.bat      # Windows 启动脚本
data/                           # 群 openid 等运行数据（已 gitignore）
AGENTS.md                       # 面向开发者的实现细节
```

## 环境要求

- Python 3.11+
- QQ 开放平台机器人（AppID + AppSecret）
- 可选：DeepSeek API Key
- 后端 `go-aliyunmc-v2` 运行在 `BACKEND_BASE_URL`（默认 `http://127.0.0.1:45678/bot-v1`，需包含 `/bot-v1` 前缀）

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

Linux / macOS 也可以直接用 `start.sh`，它会自动创建虚拟环境、按 `pyproject.toml` 安装依赖：

```bash
./start.sh                    # 开发：前台运行，日志直接打印，Ctrl+C 停止
SEATIDE_ENV=prod ./start.sh   # 生产：screen 后台运行
./start.sh restart            # 重启生产进程
./start.sh stop               # 停止生产进程
```

脚本始终以自身所在目录（项目根目录）为工作目录，无需修改路径。生产模式的日志同时写入 `data/bot.log`。

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
- 默认只有 @bot 的群消息会推送；群主开启「获取群内全部消息」后会收到 `GROUP_MESSAGE_CREATE` 全量事件
- 收到全量消息后，机器人默认仍只响应 @ 或回复它的消息；开启自动聊天后才会参与普通群聊
- 未上线的机器人只能在沙箱测试群使用，测试群人数上限 20

### 主要配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DRIVER` | `~fastapi+~httpx+~websockets` | 必填，HTTP 服务 + QQ WebSocket |
| `ENVIRONMENT` | `dev` | 由配置文件设置 |
| `LOG_LEVEL` | `DEBUG` / `INFO` | 日志级别 |
| `BACKEND_BASE_URL` | `http://127.0.0.1:45678/bot-v1` | 后端完整前缀，需包含 `/bot-v1`；bot 不会自动追加路径段 |
| `GROUP_OPENID` | 空 | 目标群 openid，留空则自动记录 |
| `QQ_BOTS` | 无 | 机器人凭据 JSON 数组 |
| `DEEPSEEK_API_KEY` | 空 | 留空则关闭 AI 对话 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | DeepSeek 接口地址 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `AI_SYSTEM_PROMPT_FILE` | 空 | 系统提示词 Markdown 文件路径；留空或文件无效时不发送 |
| `AI_TEMPERATURE` | `1.0` | 采样温度 |
| `AI_MAX_TOKENS` | `800` | 单次回复最大输出 token 数（含思考内容） |
| `AI_THINKING_EFFORT` | `low` | 思考强度：`none` 关闭思考，`low` / `high` / `max` 开启思考 |
| `AI_MAX_TURNS` | `10` | 每个用户保留的对话轮数 |
| `AI_HISTORY_TTL` | `3600` | 上下文空闲过期秒数 |
| `AI_MAX_REPLY_CHARS` | `1200` | 回复最大字符数，超出截断 |
| `AI_GROUP_CHAT_WITHOUT_AT` | `false` | 群内不 @ 也参与聊天；回复走被动消息 |
| `AI_GROUP_CHAT_PER_MINUTE` | `5` | 每个群每分钟自动聊天回复上限，0 关闭 |
| `HOST` / `PORT` | `127.0.0.1:8081` | HTTP 服务监听地址，建议只监听本机 |
| `HTTP_API_MAX_CHARS` | `2000` | 接口单条消息最大字符数 |

群 openid 不需要手动获取：机器人收到第一条群消息后会打印并写入 `data/group_openid.txt`，重启后继续生效。

## 命令

| 命令 | 别名 | 范围 | 说明 |
|---|---|---|---|
| `/状态` | `status`、`zt` | 群/私聊 | 服务器在线状态、IP、玩家数、运行时长、内存/CPU、预计释放时间 |
| `/启动` | `start`、`qd` | 群/私聊 | 触发 `oneclick_start_game` 一键启动任务 |

群内命令需要 @bot；开启自动聊天后普通聊天不需要 @，但命令仍然需要。命令不需要登录，后端只按来源地址限制访问频率。

## AI 对话

- 私聊直接发任意非命令文字
- 群内 @bot 后发任意文字
- 每次请求会带上该用户最近 `AI_MAX_TURNS` 轮上下文，空闲超过 `AI_HISTORY_TTL` 秒自动清空
- 上下文只保存在进程内存，重启后丢失
- 未配置 `DEEPSEEK_API_KEY` 时 AI 功能关闭，私聊会收到命令帮助
- 同一个用户的消息串行处理，全局并发受 `AI_MAX_CONCURRENCY` 限制
- 每次成功调用都会以 INFO 级别记录一行 `DeepSeek 用量`：prompt / completion / 思考 token / 缓存命中情况，便于观察成本构成

### 自动聊天（不 @）

在 `.env.dev` 或 `.env.prod` 中设置：

```env
AI_GROUP_CHAT_WITHOUT_AT=true
AI_GROUP_CHAT_PER_MINUTE=5
```

行为：

- 群内普通文字不需要 @ 机器人，也会进入 AI 对话
- 每个群每分钟最多自动回复 `AI_GROUP_CHAT_PER_MINUTE` 条，超出后静默跳过
- @ 机器人的消息不受该限制，随时响应
- 命令仍然需要 @bot，不带 @ 的 `/状态` 等命令不会触发
- 机器人自己和其他机器人发的消息会被忽略，避免循环
- 自动回复使用触发消息的 `msg_id` 作为被动回复，每条触发消息只回一次，不占用主动消息额度
- 自动回复是即时发生的，处于 QQ 被动回复的 5 分钟窗口内

## HTTP 接口

服务默认只监听 `127.0.0.1:8081`，随机器人一起启动。接口只接受来自本机回环地址的请求，其他来源一律返回 403。

### 发送群消息

```bash
curl -X POST http://127.0.0.1:8081/bot/group-message \
  -H "Content-Type: application/json" \
  -d '{"message": "服务器将在 10 分钟后重启"}'
```

成功响应：

```json
{"ok": true, "group_openid": "xxx", "message_id": "xxx"}
```

错误响应：

| 状态码 | 含义 |
|---|---|
| 400 | message 为空或超过 `HTTP_API_MAX_CHARS` |
| 403 | 非本机请求，或携带了转发头 |
| 502 | QQ 接口发送失败（无权限、频控等） |
| 503 | 机器人未连接、群 openid 未知 |

### 探活

```bash
curl http://127.0.0.1:8081/bot/health
# {"ok": true, "bot_ready": true, "group_configured": true}
```

同样只允许本机访问。

### 注意

- 群 openid 未知时先让群里 @bot 发一条消息，或手动填写 `GROUP_OPENID`
- QQ 对主动消息有频控和权限要求，无权限时接口返回 502
- 主动消息配额以开放平台后台为准；被动回复为 5 分钟内最多 5 条
- 接口不提供外网访问；不要改 `HOST` 或在前面加反向代理，`Forwarded` / `X-Forwarded-For` / `X-Real-IP` 请求会被拒绝

## 数据与持久化

| 路径 | 内容 |
|---|---|
| `data/group_openid.txt` | 自动记录的群 openid |

`data/`、`.env.dev`、`.env.prod`、`.venv/` 均在 `.gitignore` 中。

## 部署

- 开发机可直接双击 `run_dev.bat` / `run_prod.bat`
- Linux / macOS 用 `start.sh`，生产环境自动跑在 screen 里：

```bash
SEATIDE_ENV=prod ./start.sh          # 启动
SEATIDE_ENV=prod ./start.sh restart  # 重启
SEATIDE_ENV=prod ./start.sh stop     # 停止
```

- 也可以交给 systemd，关键点是工作目录、虚拟环境里的 `python`、以及 `SEATIDE_ENV=prod`
- 部署前确认 `.env.prod` 中 `BACKEND_BASE_URL` 指向正确的后端地址

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
- HTTP 服务只接受本机请求，不要暴露到公网或加反向代理
- AppSecret 泄露可在 QQ 开放平台重置

## 开发

更细的实现说明见 `AGENTS.md`。

```bash
# 语法检查
.venv\Scripts\python.exe -m compileall -q bot.py src/plugins

# 离线加载插件（不连接 QQ）
.venv\Scripts\python.exe -c "import nonebot; nonebot.init(_env_file='.env.dev'); from nonebot.adapters.qq import Adapter; nonebot.get_driver().register_adapter(Adapter); [nonebot.load_plugin(n) for n in ('src.plugins.lab_client','src.plugins.ai_chat','src.plugins.http_api')]; print('loaded')"
```

## 更新记录

- 2026-11-20：命令改为对接后端 `/bot-v1`（`/状态` → `server-status`，`/启动` → `trigger-one-click-start`），移除 `/登录`、`/释放时间`、`/我的信息`、`/重置对话`；插件由 `mc_client` 改名为 `lab_client`，去掉 SQLite/JWT；`/开机` 更名为 `/启动`
- 2026-11-20：适配群消息全量模式 `GROUP_MESSAGE_CREATE`；新增群内不 @ 自动聊天、每分钟限流和被动回复额度说明
- 2026-11-20：后端配置简化为 `BACKEND_BASE_URL` 一项，移除 `BOT_KEY` / `BOT_USERNAME` / `BOT_PASSWORD` 与 SSE 状态推送
- 2026-11-20：HTTP 接口移除 `HTTP_API_TOKEN`，改为只接受本机回环请求
