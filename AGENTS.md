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
| `src/plugins/lab_client/__init__.py` | 命令匹配器、未知指令兜底、群 openid 自动发现、状态格式化 |
| `src/plugins/lab_client/config.py` | 从 NoneBot 配置读取 `BACKEND_BASE_URL`、`GROUP_OPENID` |
| `src/plugins/lab_client/api.py` | 对后端机器人接口的 HTTP 封装（`ping` / `server-status` / `trigger-one-click-start`），路径直接拼在 `BACKEND_BASE_URL` 后。无鉴权，后端响应格式 `{"data": ...}` / `{"details": "..."}` |
| `src/plugins/ai/__init__.py` | 群消息入口：聊天记录落库、AI 总开关、群 @ / 自动聊天、限流、全局互斥 |
| `src/plugins/ai/config.py` | router（TypeSafe System One）/ chat / knowledge 配置，chat / knowledge 提示词与知识库 URL 加载 |
| `src/plugins/ai/api.py` | OpenAI Chat Completions 客户端（官方 `openai` SDK，固定 openai-completions 格式），支持工具调用、用量日志（chat / knowledge 使用） |
| `src/plugins/ai/router.py` | TypeSafe System One 分类：`AsyncTypeSafeClient`、`Choice` 问题、置信度与用量日志、错误映射 |
| `src/plugins/ai/agents.py` | 两个 agent 的简单 agent loop（工具调用、结果回填，轮数由模型自行结束） |
| `src/plugins/ai/tools.py` | 聊天工具（群聊记录检索、实时状态、时间）与知识库工具（llms.txt 检索、受限 fetch） |
| `src/plugins/ai/workflow.py` | 工作流编排：router → agent |
| `src/plugins/ai/chat_log.py` | 群聊聊天记录永久存储（SQLite `data/chat_log.db`），供上下文预置和 `recall_chat` 检索 |
| `src/plugins/http_api/` | 对外 HTTP 接口：`POST /bot/group-message` 向固定群发消息，`GET /bot/health` 探活；仅回环地址，随 NoneBot 的 FastAPI 驱动启动 |
| `test_mock_client.py` | [已废弃] 旧版 OneBot v11 模拟客户端，迁移至 adapter-qq 后不再可用 |

## 配置

所有配置通过 `.env` 文件，由 NoneBot 自动加载。关键变量：

| 变量 | 说明 |
|---|---|
| `DRIVER` | **必填** `~httpx+~websockets`。WS 客户端模式直连 QQ 开放平台 |
| `BACKEND_BASE_URL` | 后端完整前缀，唯一需要配置的后端参数。本机用 `http://127.0.0.1:45678/bot-v1`；API 路径直接拼在它后面，代码不自动追加 `/bot-v1` |
| `GROUP_OPENID` | 目标群 openid。首次收到群消息会自动记录并写入 `data/group_openid.txt`，重启后继续生效；也可手动填入固定使用 |
| `QQ_BOTS` | JSON 数组，含 bot 的 `id`、`token`、`secret`、`intent` |
| `AI_ENABLED` | 整个 AI 链路唯一开关，默认 `true`；`false` 时不调用任何模型，非指令消息回复「暂未启动AI」 |
| `AI_DEFAULT_API_KEY` | chat / knowledge 未单独配置 `API_KEY` 时的默认 Key；router 不使用 |
| `AI_DEFAULT_BASE_URL` | chat / knowledge 未单独配置 `BASE_URL` 时的默认端点（API 根地址，SDK 自动拼接 `/chat/completions`），默认 `https://api.deepseek.com` |
| `AI_DEFAULT_MODEL` | chat / knowledge 未单独配置 `MODEL` 时的默认模型 id，默认 `deepseek-chat` |
| `AI_DEFAULT_TEMPERATURE` | chat / knowledge 未单独配置温度时的默认值，默认 `1.0` |
| `AI_DEFAULT_MAX_TOKENS` | chat / knowledge 未单独配置最大输出 token 时的默认值，默认 `800` |
| `AI_DEFAULT_THINKING_EFFORT` | chat / knowledge 未单独配置思考强度时的默认值，默认 `low` |
| `AI_DEFAULT_TIMEOUT` | chat / knowledge 未单独配置超时时的默认值，默认 `60` 秒 |
| `AI_ROUTER_*` | TypeSafe System One router：`AI_ROUTER_API_KEY` 必填；`BASE_URL` 默认 `https://api.typesafe.ai`，用阿里云百炼时填 `https://{WorkspaceId}.{region}.maas.aliyuncs.com/compatible-mode`；`MODEL` 默认 `jev-latest`（百炼填 `decision-model-preview`）；另有 `TIMEOUT`、`MAX_RETRIES`、`HISTORY_TURNS` |
| `AI_CHAT_*` | 普通聊天 agent 配置，`AI_CHAT_API_KEY` 可回退 `AI_DEFAULT_API_KEY`；其余字段同上 |
| `AI_KNOWLEDGE_*` | 知识库 agent：`AI_KNOWLEDGE_API_KEY` 可回退 `AI_DEFAULT_API_KEY`；其余模型字段同上，另有 `LLMS_TXT_URL`、`FETCH_TIMEOUT`、`CACHE_TTL`、`FETCH_URL_REGEX` |
| `AI_CHAT_LOG_RECENT` | 群聊上下文预置的最近聊天记录条数，默认 20，最大 200 |
| `AI_ROUTER_MAX_RETRIES` | router 的 SDK 自动重试次数（不含首次请求），默认 2，0 关闭 |
| `AI_ROUTER_HISTORY_TURNS` | router 参考最近多少轮对话，默认 3 |
| `AI_GROUP_CHAT_WITHOUT_AT` | 群内不 @ 是否参与聊天，默认 false |
| `AI_GROUP_CHAT_PER_MINUTE` | 每个群每分钟自动聊天回复上限，默认 5 |
| `HOST` / `PORT` | HTTP 服务监听地址，默认 `127.0.0.1:8081`，建议只监听本机 |
| `HTTP_API_MAX_CHARS` | 单条消息最大字符数，默认 2000 |

## 命令

所有命令通过 `on_command` 匹配，支持中文主名 + 英文别名：

| 命令 | 别名 | 权限 | 说明 |
|---|---|---|---|
| `/状态` | `status`, `zt` | 群聊（@bot） | 调 `GET /bot-v1/server-status`：在线状态、IP、玩家数、运行时长、内存/CPU、预计释放时间 |
| `/启动` | `start`, `qd` | 群聊（@bot） | 调 `POST /bot-v1/trigger-one-click-start`；任务已在跑时提示无需重复触发 |

命令不需要登录；后端按来源地址限流，`/启动` 遇到 `409`（其他部署链任务执行中）或 `429` 会直接回复原因。

**指令判定**：任何以 `/` 或 `／` 开头的消息都视为指令，无论该指令是否存在。已知指令由 `on_command` 处理；未知指令由 `lab_client` 的兜底匹配器回复「指令不存在」并列出可用指令。群内指令仍然需要 @bot，未 @ 的斜杠消息会被忽略。

**非指令消息**：只有群消息进入 `ai` 工作流（私聊直接忽略）。所有群消息先写入 `data/chat_log.db`；AI 关闭时回复「暂未启动AI」，AI 开启时从聊天记录预置上下文并交给 router 判断聊天类型。

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
- `BACKEND_BASE_URL` 是完整前缀（默认 `http://127.0.0.1:45678/bot-v1`），`api.py` 只拼接 `/ping`、`/server-status`、`/trigger-one-click-start` 这类接口路径，**不会**自动追加 `/bot-v1`。
- `/bot-v1` 不签发用户登录态，bot 不再保存用户名/密码/JWT，任何人都能触发 `/启动`，仅受后端 `rate_limit_per_minute` 限制。

## 约定与踩坑

- `load_plugin("src.plugins.lab_client")` 指定模块名加载；`load_plugins("src.plugins")` 目录扫描无法发现子包
- 必须 `DRIVER=~httpx+~websockets`；缺少 websockets 则无法建立 WS 连接
- `data/` 目录需手动 `mkdir`，`data/group_openid.txt` 依赖它存在
- `/bot-v1/server-status` 的字段是 camelCase（`uptimeSeconds`、`usagePercent`、`usedCores`、`release.inSeconds` 等），且 `uptimeSeconds`、`memory`、`cpu`、`players.max` 可能为 `null`，格式化前要判空
- 后端错误响应字段名是 `details` 不是 `error`
- QQ 官方 API 用 `openid` 标识用户和群，不是 QQ 号
- 群主开启「获取群内全部消息」后，事件类型从 `GROUP_AT_MESSAGE_CREATE` 变为 `GROUP_MESSAGE_CREATE`；插件统一用 `GroupMessageCreateEvent` 基类，并用 `event.to_me` / `mentions.is_you` 判断是否被 @
- `QQ_BOTS` 的 intent 只开了 `c2c_group_at_messages`，bot 会收到私聊和群 @ 消息；目前私聊事件直接忽略，只处理群聊
- `GROUP_OPENID` 首次为空是正常的：启动后在群里 @bot，从日志复制 openid 填入后重启即可
- `.env`、`data/`、`.venv/` 均被 `.gitignore` 排除
- router 依赖 `typesafe-sdk`（0.7.x，内部使用 `httpx2`）；它只负责 System One，不参与 openai-completions 调用
- 阿里云百炼决策模型（`AI_ROUTER_MODEL=decision-model-preview`）的端点是 `https://{WorkspaceId}.{region}.maas.aliyuncs.com/compatible-mode/v1/systemone`；SDK 会在 `AI_ROUTER_BASE_URL` 后追加 `/v1/systemone`，所以 BASE_URL 必须以 `/compatible-mode` 结尾。写成 `/compatible-mode/v1` 会请求 `/compatible-mode/v1/v1/systemone` 返回 404，启动校验会直接拦下 `*.maas.aliyuncs.com` 但不以 `/compatible-mode` 结尾的配置
- chat / knowledge 通过官方 `openai` SDK 调用，API 格式固定为 openai-completions；各 `AI_*_BASE_URL` 填 API 根地址（如 `https://api.deepseek.com` 或 `https://example.com/v1`），SDK 自动拼接 `/chat/completions`，旧配置写成完整端点时本模块会自动去掉该后缀
- `httpx` 仍用于知识库的 llms.txt / 页面抓取，与 openai SDK 内部的 httpx 客户端互不共用
- System One 的 `state` / `questions` 必须是 JSON 可序列化对象；最近对话直接复用 `chat_log.recent_messages()` 的 `{role, content}` 结构，role 为 `user` / `assistant`
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

## AI 工作流

整体链路：

```
非指令消息
   │
   ├─ AI 关闭 → 回复「暂未启动AI」
   │
   └─ AI 开启 → router（TypeSafe System One）
                   │
                   ├─ chat      → chat agent      → 最终回复
                   └─ knowledge → knowledge agent → 最终回复
```

- **指令优先**：斜杠消息不会进入 AI；未知指令由 `lab_client` 回复「指令不存在」。
- **启动校验**：`AI_ENABLED=true` 时在启动阶段检查 router 的 API Key、BASE_URL、MODEL、timeout，以及 chat / knowledge 生效后的 API Key、BASE_URL、MODEL、系统提示词、temperature / timeout，还有 `AI_KNOWLEDGE_LLMS_TXT_URL` 是否存在且为 http(s) URL；启动时还会实际拉取一次 llms.txt，确认可访问且能解析出条目。任何一项不通过都会抛出 `RuntimeError`，直接阻止启动，不把配置错误留到运行时。
- **router**：通过 `typesafe-sdk` 调用 TypeSafe System One（`POST /v1/systemone`），不走 openai-completions。请求的 `state` 是 `{"recent_conversation": 最近 AI_ROUTER_HISTORY_TURNS 轮记录, "latest_message": 最新消息}`，`questions` 只有一个名为 `intent` 的 `Choice` 问题，criteria 固定为 `chat` / `knowledge`，不发送 `instructions` 或其他提示词。取 `response.choices["intent"].choice`，并校验必须严格等于两个 intent 之一（无别名、无兜底解析）；调用失败、答案类型不是 choice 或 intent 未知时直接报错，不切换到其他 agent。System One 没有 temperature / thinking effort / max_tokens，重试由 SDK `RetryPolicy` 负责。
- **chat agent**：系统提示词默认 `prompts/chat_prompt.md`，工具只有 `recall_chat`。乱码、无意义内容等无法归入知识库的消息也由它处理。
- **knowledge agent**：系统提示词默认 `prompts/knowledge_prompt.md`，工具为 `search_knowledge`、`fetch_doc`、`get_server_status`、`get_current_time`。`search_knowledge` 拉取 `AI_KNOWLEDGE_LLMS_TXT_URL` 指向的 llms.txt 并检索条目（不传 query 时返回全部条目，`limit` 控制最多返回条数，检索默认 5 条）；`fetch_doc` 只允许抓取索引中出现过的 URL，返回 Markdown / 文本 / HTML 提取后的正文。需要实时服务器状态或当前时间的场景也由 knowledge agent 负责。
- **提示词文件**：chat / knowledge 各用一个独立的提示词文件，支持 `{{include: shared/xxx.md}}` 复用共享片段：路径相对当前提示词文件所在目录，支持嵌套，循环与最大 8 层嵌套会报错；展开失败会使系统提示词为空，由启动校验直接阻止启动。router 不使用提示词文件，System One 请求只发送 state 和 Choice 问题。
- **agent loop**：工具结果原样写回模型，不截断；工具调用轮数不设上限，由模型自行决定何时给出最终回复。
- **模型配置**：router 与 chat / knowledge 两套配置互相独立。router 只读取 `AI_ROUTER_*`：`AI_ROUTER_API_KEY` 必填，`BASE_URL` / `MODEL` 未配置时使用 `https://api.typesafe.ai` / `jev-latest`；超时和重试分别由 `AI_ROUTER_TIMEOUT` / `AI_ROUTER_MAX_RETRIES` 控制，不使用 `AI_DEFAULT_*`。chat / knowledge 固定通过官方 `openai` SDK 走 openai-completions 格式；它们的 API Key 未单独配置时回退到 `AI_DEFAULT_API_KEY`；端点、模型 id、temperature、thinking effort、max tokens、超时和系统提示词未配置时回退到 `AI_DEFAULT_*`。
- **上下文**：只按群隔离，不按人隔离。所有群消息（含指令、未 @ 闲聊）落地到 `data/chat_log.db`；每次请求从该表预置最近 `AI_CHAT_LOG_RECENT` 条作为共享上下文。router 和两个 agent 共享同一份上下文，chat agent 可用 `recall_chat` 按关键词和时间范围（`Nd` / `Nh` / `Nm`，默认 `1h`）检索更早的记录；格式无法解析、或起点早于本群最早聊天记录时，工具会返回错误而不是静默调整范围，`0` 表示不限时间。
- **并发与限流**：全局互斥，同一时间只跑一条 AI 链路；处理中收到的 @ 消息立即回复「上一条消息正在处理中，请稍后再问」，未 @ 的自动聊天消息直接跳过。开启 `AI_GROUP_CHAT_WITHOUT_AT` 后，未 @ 的群消息另按群每分钟限流，回复走被动消息。
- **私聊**：暂不支持。私聊事件会被直接忽略，指令和 AI 都只处理群聊。
- **日志**：每次路由以 INFO 记录 `AI 路由：chat/knowledge` 和 System One 的 `confidence`，DEBUG 记录两类概率；chat / knowledge 的每次模型调用记录 `LLM 用量[component/model]`（prompt / completion / 思考 token / 缓存命中），router 另行记录 prompt / completion token 用量。
- **知识库**：标准 llms.txt 索引（`# 标题`、`> 简介`、`## 章节`、`- [标题](URL): 说明`）；没有链接时把 llms.txt 自身当作可 fetch 的文档。索引与页面按 `AI_KNOWLEDGE_CACHE_TTL` 秒缓存，支持 Markdown、纯文本和 HTML，HTML 会去掉脚本/导航/页脚。
- **抓取安全**：`fetch_doc` 只允许抓取 llms.txt 索引中出现过的 URL，以及 http(s) 协议；配置 `AI_KNOWLEDGE_FETCH_URL_REGEX` 后，URL 还必须完整匹配该正则（作为额外白名单），留空则不额外限制。重定向逐跳校验协议和正则；若文档会 301 到其他域名，正则必须同时放行目标域名（例如 Seatide 文档从 `seatidemc.github.io` 301 到 `http://seatide.net`）。不再限制回环/内网地址。
- 调用失败（超时 / 401 / 402 / 429 / router 失败 / System One 返回未知 intent）会把原因直接回复给用户，不进行 agent 切换。

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
