# Seatide Bot

Seatide Minecraft 服务器的 QQ 群机器人，基于 [NoneBot2](https://nonebot.dev/) 和 QQ 官方机器人 API（WebSocket 客户端模式），通过官方 OpenAI SDK 接入 openai-completions 模型（默认 DeepSeek）做多轮对话与知识库问答，消息路由使用 TypeSafe System One，并对外提供一个发送群消息的 HTTP 接口。

机器人默认只服务一个群。

## 功能

- 群内命令：服务器状态（在线、人数、资源占用、预计释放时间）、一键启动服务器
- 斜杠消息统一按指令处理；未知指令会明确回复「指令不存在」
- AI 工作流：router 使用 TypeSafe System One 自动区分普通聊天与知识库问答，分别交给独立 agent 处理
- chat / knowledge 模型可分别配置端点、模型 id、温度、思考强度和提示词；router 可单独配置 API Key、端点、模型、超时和重试次数
- agent 支持工具调用：chat agent 可检索群聊记录；knowledge agent 可查实时服务器状态、当前时间，并检索 llms.txt 知识库与抓取页面
- 群内自动聊天支持每分钟限流，回复走被动消息，不占用主动消息额度
- 支持群消息全量模式（GROUP_MESSAGE_CREATE），只响应 @、回复或自动聊天
- HTTP 接口：后端或脚本调用后在群内发消息
- 两套配置：`.env.dev`（开发）与 `.env.prod`（生产）

## 项目结构

```
bot.py                          # 入口，选择环境并加载插件
src/plugins/lab_client/         # QQ 命令（对接后端 /bot-v1）+ 未知指令兜底
src/plugins/ai/                # AI 工作流：System One router、chat/knowledge agent、工具
src/plugins/http_api/           # 对外 HTTP 接口
prompts/                        # chat / knowledge 提示词
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
- AI 开启时：TypeSafe API Key（router）和 openai-completions 模型的 API Key（chat / knowledge，默认 DeepSeek）
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

填好两份 `.env` 中的 QQ 凭据和模型 API Key 后，双击 `run_dev.bat` 或 `run_prod.bat`。

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
4. 开通事件订阅：群聊 @ 消息（目前不支持私聊，私聊事件会被忽略）
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
| `GROUP_OPENID` | 空 | 默认目标群 openid；留空则自动记录收到的群（支持多个） |
| `QQ_BOTS` | 无 | 机器人凭据 JSON 数组 |
| `AI_ENABLED` | `true` | 整个 AI 链路唯一开关；`false` 时非指令消息回复「暂未启动AI」；`true` 时启动阶段校验配置 |
| `AI_DEFAULT_API_KEY` | 空 | chat / knowledge 未单独配置 `API_KEY` 时的默认 Key；router 不使用 |
| `AI_DEFAULT_BASE_URL` | `https://api.deepseek.com` | chat / knowledge 未单独配置 `BASE_URL` 时的默认端点 |
| `AI_DEFAULT_MODEL` | `deepseek-chat` | chat / knowledge 未单独配置 `MODEL` 时的默认模型名 |
| `AI_DEFAULT_TEMPERATURE` | `1.0` | chat / knowledge 未单独配置 `TEMPERATURE` 时的默认温度 |
| `AI_DEFAULT_MAX_TOKENS` | `800` | chat / knowledge 未单独配置 `MAX_TOKENS` 时的默认最大输出 token |
| `AI_DEFAULT_THINKING_EFFORT` | `low` | chat / knowledge 未单独配置思考强度时的默认值：`none` / `low` / `high` / `max` |
| `AI_DEFAULT_TIMEOUT` | `60` | chat / knowledge 未单独配置 `TIMEOUT` 时的默认请求超时秒数 |
| `AI_ROUTER_*` | 见 `.env.example` | TypeSafe System One router：`API_KEY`（必填）/ `BASE_URL`（API 根地址，默认 `https://api.typesafe.ai`，SDK 自动追加 `/v1/systemone`）/ `MODEL`（默认 `jev-latest`）/ `TIMEOUT` / `MAX_RETRIES` / `HISTORY_TURNS` / `MIN_KNOWLEDGE_PROBABILITY` |
| `AI_CHAT_*` | 见 `.env.example` | chat agent 模型配置，字段同上；`API_KEY` 可回退 `AI_DEFAULT_API_KEY` |
| `AI_KNOWLEDGE_*` | 见 `.env.example` | knowledge agent 模型配置，`API_KEY` 可回退 `AI_DEFAULT_API_KEY`；另有 `LLMS_TXT_URL` / `FETCH_TIMEOUT` / `CACHE_TTL` |
| `AI_CHAT_SYSTEM_PROMPT_FILE` | `prompts/chat_prompt.md` | chat agent 系统提示词 |
| `AI_KNOWLEDGE_SYSTEM_PROMPT_FILE` | `prompts/knowledge_prompt.md` | knowledge agent 系统提示词 |
| `AI_ROUTER_MAX_RETRIES` | `2` | router 的 SDK 自动重试次数（不含首次请求），0 表示不重试 |
| `AI_KNOWLEDGE_LLMS_TXT_URL` | 空 | 知识库 llms.txt 索引 URL，必须是 http/https；AI 开启时必填 |
| `AI_KNOWLEDGE_FETCH_TIMEOUT` | `15` | 抓取 llms.txt / 页面的超时秒数 |
| `AI_KNOWLEDGE_CACHE_TTL` | `300` | 索引/页面内存缓存秒数，0 表示不缓存 |
| `AI_KNOWLEDGE_FETCH_URL_REGEX` | 空 | `fetch_doc` 允许抓取的 URL 正则（完整匹配）；空表示只受 llms.txt 索引约束，重定向逐跳校验。文档 301 到其他域名时，目标域名也必须被正则放行 |
| `AI_CHAT_LOG_RECENT` | `20` | 群聊上下文预置的最近聊天记录条数，最大 200 |
| `AI_CHAT_LOG_WINDOW_MINUTES` | `30` | 群聊上下文只取最近多少分钟内的记录，0 表示不限时间 |
| `AI_ROUTER_HISTORY_TURNS` | `3` | router 参考最近多少条用户消息（跳过 bot 回复和斜杠指令），用于理解追问和指代 |
| `AI_ROUTER_MIN_KNOWLEDGE_PROBABILITY` | `0.6` | knowledge 概率低于该值时按 chat 处理，取值 0~1；0 表示不启用 |
| `AI_GROUP_CHAT_WITHOUT_AT` | `false` | 群内不 @ 也参与聊天；回复走被动消息 |
| `AI_GROUP_CHAT_PER_MINUTE` | `5` | 每个群每分钟自动聊天回复上限，0 关闭 |
| `AI_MARKDOWN_REPLY` | `true` | AI 最终回复以 QQ 原生 Markdown（`msg_type=2`）发送；发送失败自动回退纯文本 |
| `HOST` / `PORT` | `127.0.0.1:8081` | HTTP 服务监听地址，建议只监听本机 |
| `HTTP_API_MAX_CHARS` | `2000` | 接口单条消息最大字符数 |

群 openid 不需要手动获取：机器人收到群消息后会打印并写入 `data/group_openids.json`，重启后继续生效。机器人被拉入多个群时会记录多个 openid，AI 上下文和聊天记录按群隔离，互不干扰。

## 命令

| 命令 | 别名 | 范围 | 说明 |
|---|---|---|---|
| `/状态` | `status`、`zt` | 群聊（@bot） | 服务器在线状态、IP、玩家数、运行时长、内存/CPU、预计释放时间 |
| `/启动` | `start`、`qd` | 群聊（@bot） | 触发 `oneclick_start_game` 一键启动任务 |

群内命令需要 @bot；开启自动聊天后普通聊天不需要 @，但命令仍然需要。命令不需要登录，后端只按来源地址限制访问频率。

任何以 `/` 或 `／` 开头的消息都会被视为指令，无论它是否存在。不存在的指令会收到「指令不存在」和可用指令列表。非指令消息才会进入 AI。

## AI 工作流

消息处理顺序：

1. 以 `/` 或 `／` 开头 → 指令；已知指令执行对应命令，未知指令回复「指令不存在」。
2. 非指令消息 → 进入 AI。AI 关闭（`AI_ENABLED=false`）时回复「暂未启动AI」。
3. AI 开启 → router（TypeSafe System One 分类器）判断是普通聊天还是知识库问答。
4. 普通聊天（包括乱码、无意义内容等无法归入知识库的消息）交给 chat agent，知识库问答交给 knowledge agent；各自有独立的系统提示词、模型配置和工具集。
5. chat agent 可检索群聊记录；knowledge agent 可检索知识库、抓取页面，并查询实时服务器状态和当前时间。由模型自行决定何时结束工具调用并给出最终回复。模型偶发返回空正文时会自动重试最多 2 次，并记录 `finish_reason` 便于排查。

补充说明：

- 只支持群聊：@bot 后发文字；开启自动聊天后，未 @ 的普通文字也会进入 AI。私聊消息直接忽略。
- 群聊记录永久保存在 `data/chat_log.db`（含指令和未 @ 的闲聊），上下文按群共享、不按人隔离；每次请求预置最近 `AI_CHAT_LOG_RECENT` 条，更早的内容由 chat agent 调用 `recall_chat` 按关键词和时间范围（如 `30m` / `12h` / `7d`）检索。
- 按群互斥：同一个群同一时间只跑一条 AI 链路，不同群可以并行处理。处理中收到的同群 @ 消息会立即回复「上一条消息正在处理中，请稍后再问」，同群未 @ 的自动聊天消息直接跳过。聊天记录、上下文和工具检索按群隔离。
- AI 最终回复默认以 QQ 原生 Markdown（`msg_type=2`）发送，`AI_MARKDOWN_REPLY=false` 可关闭；发送失败会自动回退纯文本。QQ 原生 Markdown 语法有限（标题、粗体/斜体、链接、列表、引用、分割线等），模型输出代码块时可能按普通文本显示。
- 启动校验：`AI_ENABLED=true` 时会检查 router 的 API Key、BASE_URL、MODEL、timeout、knowledge 概率门槛，以及 chat / knowledge 的 API Key、BASE_URL、MODEL、系统提示词、temperature / timeout 和 `AI_KNOWLEDGE_LLMS_TXT_URL`；启动时还会实际拉取一次 llms.txt，确认可访问且能解析出条目。任何一项失败都会直接阻止启动，不会留到运行时降级。
- router 通过 `typesafe-sdk` 调用 System One，不使用 openai-completions。若判为 knowledge 但 knowledge 概率低于 `AI_ROUTER_MIN_KNOWLEDGE_PROBABILITY`（默认 0.6），按 chat 处理，避免把闲聊送进知识库 agent。调用失败（超时 / 401 / 429 等）或返回的 intent 不在 `chat` / `knowledge` 中时不切换到其他 agent；具体错误只写日志，群内只回复统一提示（429 为「请求太频繁」，其余为「AI 暂时不可用」），不暴露内部报错。
- chat / knowledge 固定使用 OpenAI Chat Completions 格式（openai-completions），通过官方 `openai` SDK 调用；对应的 `AI_*_BASE_URL` 填 API 根地址（如 `https://api.deepseek.com` 或 `https://example.com/v1`），SDK 会自动拼接 `/chat/completions`。
- 每次路由以 INFO 记录 `AI 路由：chat/knowledge` 和 System One 的置信度；chat / knowledge 的每次模型调用记录 `LLM 用量[component/model]`：prompt / completion / 思考 token / 缓存命中情况，便于观察成本构成；最终发到群里的回复以 INFO 完整记录（`AI 回复[intent][群 openid]：...`），不截断。
- chat / knowledge 的 API Key 可以分别配置；未配置时回退到 `AI_DEFAULT_API_KEY`（共用同一 Key 时只需填这一项）。router 的 Key 不参与这个回退，必须配置 `AI_ROUTER_API_KEY`。端点、模型 id、temperature、thinking effort、max tokens、超时和提示词也可以分别配置，未配置时回退到 `AI_DEFAULT_*` 默认值。
- chat / knowledge 各使用一个独立的提示词文件，支持 `{{include: 相对路径}}` 复用共享片段（路径相对当前提示词文件，支持嵌套，带循环与深度保护）：把公共内容放到如 `prompts/shared/xxx.md`，在多个提示词里用 `{{include: shared/xxx.md}}` 引用即可，无需手工同步。include 失败（文件缺失、循环、嵌套过深）会让系统提示词为空，启动校验直接失败。router 不使用提示词文件，System One 请求只包含 state 和 Choice 问题。

router 与各 agent 可以指向不同服务，例如：

```env
# router：TypeSafe System One，选择 chat / knowledge
AI_ROUTER_API_KEY=ts-router
AI_ROUTER_BASE_URL=https://api.typesafe.ai
AI_ROUTER_MODEL=jev-latest

# chat：日常对话
AI_CHAT_API_KEY=sk-chat
AI_CHAT_BASE_URL=https://api.deepseek.com
AI_CHAT_MODEL=deepseek-chat
AI_CHAT_TEMPERATURE=1.0

# knowledge：知识库问答，可以接别的 openai-completions 端点
AI_KNOWLEDGE_API_KEY=sk-knowledge
AI_KNOWLEDGE_BASE_URL=https://your-endpoint.example/v1
AI_KNOWLEDGE_MODEL=your-model
AI_KNOWLEDGE_TEMPERATURE=0.3
```

router 的 `AI_ROUTER_API_KEY` 必填；chat / knowledge 未填 `API_KEY` 时回退到 `AI_DEFAULT_API_KEY`，未填写 `BASE_URL` / `MODEL` 时回退到 `AI_DEFAULT_BASE_URL` / `AI_DEFAULT_MODEL`。

### 知识库（llms.txt + fetch）

知识库不放在本地，而是由一个 `llms.txt` 索引加按需抓取页面组成。把索引地址填到：

```env
AI_KNOWLEDGE_LLMS_TXT_URL=https://docs.example.com/llms.txt
```

`llms.txt` 使用标准 Markdown 结构：

```markdown
# Seatide 知识库
> 服务器规则、玩法和插件说明

## 规则
- [白名单与作弊](https://docs.example.com/rules.md): 白名单申请方式、作弊处罚
- [建筑保护](https://docs.example.com/build.md): 出生点保护范围

## 插件
- [EssentialsX](https://docs.example.com/essentials.md) - 常用传送和经济指令
```

工作方式：

1. knowledge agent 先调用 `search_knowledge`，拉取并检索 llms.txt，返回匹配条目的标题、URL 和说明；不传 `query` 时直接列出全部条目（知识库页面较少时便于先看全貌），`limit` 控制最多返回条数（检索默认 5 条，不传 `query` 时默认全部）。
2. 模型再用 `fetch_doc` 抓取相关页面正文，回答用户问题。
3. 页面支持 Markdown、纯文本和 HTML；HTML 会自动提取正文并去掉脚本、导航、页脚。
4. 索引和页面默认缓存 `AI_KNOWLEDGE_CACHE_TTL` 秒。
5. `fetch_doc` 只允许抓取 llms.txt 中列出的 URL，不能抓取任意地址。
6. `AI_ENABLED=true` 时启动阶段会检查 `AI_KNOWLEDGE_LLMS_TXT_URL` 是否存在且为 http(s) URL，并实际拉取一次 llms.txt；拉取失败或解析不到条目会直接启动失败。

### 自动聊天（不 @）

在 `.env.dev` 或 `.env.prod` 中设置：

```env
AI_GROUP_CHAT_WITHOUT_AT=true
AI_GROUP_CHAT_PER_MINUTE=5
```

行为：

- 群内普通文字不需要 @ 机器人，也会进入 AI 工作流
- 每个群每分钟最多自动回复 `AI_GROUP_CHAT_PER_MINUTE` 条，超出后静默跳过
- @ 机器人的消息不受该限制，随时响应
- 命令仍然需要 @bot，不带 @ 的 `/状态` 等命令不会触发
- 机器人自己和其他机器人发的消息会被忽略，避免循环
- 自动回复使用触发消息的 `msg_id` 作为被动回复，每条触发消息只回一次，不占用主动消息额度
- 自动回复是即时发生的，处于 QQ 被动回复的 5 分钟窗口内

## HTTP 接口

服务默认只监听 `127.0.0.1:8081`，随机器人一起启动。接口只接受来自本机回环地址的请求，其他来源一律返回 403。

### 发送群消息

请求体字段：`message`（必填，纯文本）、`group_openid`（可选，目标群）。不传 `group_openid` 时使用默认目标群（`GROUP_OPENID`，或唯一自动发现的群）；发现多个群时必须显式指定。

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
| 400 | message 为空或超过 `HTTP_API_MAX_CHARS`；群 openid 未知；发现多个群但未指定 `group_openid` |
| 403 | 非本机请求，或携带了转发头 |
| 502 | QQ 接口发送失败（无权限、频控等） |
| 503 | 机器人未连接、尚未发现任何群 |

### 探活

```bash
curl http://127.0.0.1:8081/bot/health
# {"ok": true, "bot_ready": true, "group_configured": true, "group_openids": ["..."], "default_group_openid": "..."}
```

同样只允许本机访问。`group_openids` 是已知的全部群，`default_group_openid` 是未指定群时实际使用的目标（存在多个群且未配置 `GROUP_OPENID` 时为 `null`）。

### 注意

- 群 openid 未知时先让群里 @bot 发一条消息，或手动填写 `GROUP_OPENID`；多群场景下调用发送接口需带上 `group_openid`
- QQ 对主动消息有频控和权限要求，无权限时接口返回 502
- 主动消息配额以开放平台后台为准；被动回复为 5 分钟内最多 5 条
- 接口不提供外网访问；不要改 `HOST` 或在前面加反向代理，`Forwarded` / `X-Forwarded-For` / `X-Real-IP` 请求会被拒绝

## 数据与持久化

| 路径 | 内容 |
|---|---|
| `data/group_openids.json` | 自动记录的群 openid 列表（最近发言的群在末尾） |

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
.venv\Scripts\python.exe -c "import nonebot; nonebot.init(_env_file='.env.dev'); from nonebot.adapters.qq import Adapter; nonebot.get_driver().register_adapter(Adapter); [nonebot.load_plugin(n) for n in ('src.plugins.lab_client','src.plugins.ai','src.plugins.http_api')]; print('loaded')"
```

## 更新记录

- 2026-11-22：移除 invalid agent 和 invalid intent；router 只分流 chat / knowledge，乱码等无意义消息交给 chat agent 处理
- 2026-11-22：router 改用 TypeSafe System One（`typesafe-sdk`）：用一个 `Choice` 问题把消息分为 chat / knowledge / invalid，并记录置信度；新增 `AI_ROUTER_BASE_URL` / `AI_ROUTER_MODEL` / `AI_ROUTER_TIMEOUT` / `AI_ROUTER_MAX_RETRIES`，移除 router 的 temperature / thinking effort / max tokens / 提示词配置，请求只发送 state 和 Choice 问题；`AI_ROUTER_API_KEY` 为必填项
- 2026-11-22：AI 模块改为工作流：`/` 开头统一视为指令（未知指令回复不存在），非指令消息经 router 分流到 chat / knowledge 两个 agent；新增工具调用循环、llms.txt 知识库索引与页面抓取；三个模型可分别配置，全局 fallback 统一为 `AI_DEFAULT_*`
- 2026-11-20：命令改为对接后端 `/bot-v1`（`/状态` → `server-status`，`/启动` → `trigger-one-click-start`），移除 `/登录`、`/释放时间`、`/我的信息`、`/重置对话`；插件由 `mc_client` 改名为 `lab_client`，去掉 SQLite/JWT；`/开机` 更名为 `/启动`
- 2026-11-20：适配群消息全量模式 `GROUP_MESSAGE_CREATE`；新增群内不 @ 自动聊天、每分钟限流和被动回复额度说明
- 2026-11-20：后端配置简化为 `BACKEND_BASE_URL` 一项，移除 `BOT_KEY` / `BOT_USERNAME` / `BOT_PASSWORD` 与 SSE 状态推送
- 2026-11-20：HTTP 接口移除 `HTTP_API_TOKEN`，改为只接受本机回环请求
