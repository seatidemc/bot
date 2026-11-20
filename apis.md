# Bot HTTP API

机器人除 QQ WebSocket 外，还随 NoneBot 的 FastAPI 驱动启动一个 HTTP 服务，供后端 `go-aliyunmc-v2` 或本机脚本触发消息。

- 实现：`src/plugins/http_api/__init__.py`
- 配置：`src/plugins/http_api/config.py`
- 依赖 `DRIVER` 中包含 `~fastapi`（默认 `~fastapi+~httpx+~websockets`）；驱动无 HTTP server 时接口不注册，仅打出一条 warning

## 基础信息

| 项 | 值 |
|---|---|
| Base URL | `http://{HOST}:{PORT}` |
| 默认监听 | `127.0.0.1:8081`（`.env.dev` / `.env.example` 中的 `HOST`、`PORT`） |
| 请求格式 | `application/json` |
| 响应格式 | `application/json` |
| 鉴权 | 无 token，仅按来源地址校验（见下） |

## 访问控制

服务默认只监听回环地址，路由内部还有一层校验 `_require_loopback`：

1. 请求头中**任意**出现 `Forwarded` / `X-Forwarded-For` / `X-Real-IP`，直接 `403` —— 即使代理就在本机也拒绝，防止绕过来源检查；
2. `request.client.host` 必须是回环地址（`127.0.0.1`、`::1` 等），否则 `403`。

因此：

- 只有**同一台机器上的进程**能调用，局域网其他主机即使把 `HOST` 改成 `0.0.0.0` 也会被 403 拦下；
- 不要把 `HOST` 改成 `0.0.0.0` 后经反向代理暴露到公网。

## 通用错误格式

所有错误响应均为 FastAPI 标准结构：

```json
{"detail": "错误原因"}
```

| 状态码 | 含义 |
|---|---|
| 400 | 请求参数不合法（如 `message` 为空或超长） |
| 403 | 非本机回环请求，或携带了转发头 |
| 422 | 请求体结构不符合 schema（如缺少 `message` 字段） |
| 502 | 调用 QQ 接口失败 |
| 503 | 机器人未连接 / 未就绪 / 目标群未知 |

---

## POST /bot/group-message

向固定群发送一条纯文本消息。目标群取自 `GROUP_OPENID`；未配置时使用首次收到群消息自动记录的 openid（持久化在 `data/group_openid.txt`）。

### 请求

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `message` | string | 是 | 消息文本，服务端会 `strip()`；最大 `HTTP_API_MAX_CHARS`（默认 2000）字符 |

```bash
curl -X POST http://127.0.0.1:8081/bot/group-message \
  -H "Content-Type: application/json" \
  -d '{"message": "服务器将在 10 分钟后重启"}'
```

### 成功响应 `200`

```json
{
  "ok": true,
  "group_openid": "xxxxxxxxxxxxxxxx",
  "message_id": "xxxxxxxxxxxxxxxx"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 固定为 `true` |
| `group_openid` | string | 实际发送到的群 openid |
| `message_id` | string \| null | QQ 返回的消息 id |

### 错误响应

| 状态码 | detail 示例 | 触发条件 |
|---|---|---|
| 400 | `message 不能为空` | `message` 去除空白后为空 |
| 400 | `message 超长，最多 2000 字符` | 长度超过 `HTTP_API_MAX_CHARS` |
| 403 | `仅允许本机访问` | 非回环来源或带转发头 |
| 422 | FastAPI 校验信息 | 缺少 `message` 字段、类型不是字符串 |
| 502 | `QQ 发送失败：<原因>` | QQ 开放平台返回错误（权限、频率限制等） |
| 503 | `群 openid 未知：请配置 GROUP_OPENID，或先在群里 @bot 发一条消息` | 尚未确定目标群 |
| 503 | `QQ 机器人尚未连接` | 没有已连接的 bot 实例 |
| 503 | `QQ 机器人尚未就绪` | bot 已连接但 `ready` 为 false |

### 注意

- 该接口使用**主动消息**：QQ 开放平台对主动推送有频率和权限限制，需要平台侧开通能力；被动回复的限制是 5 分钟内 5 条。
- 返回 502 时先检查开放平台的消息权限与配额，看 bot 日志中的 `HTTP 接口发群消息失败` 记录。
- 只支持纯文本，不支持 markdown / 图片 / 按钮等富媒体。

---

## GET /bot/health

探活接口，用于后端或脚本确认机器人状态。

```bash
curl http://127.0.0.1:8081/bot/health
```

### 成功响应 `200`

```json
{
  "ok": true,
  "bot_ready": true,
  "group_configured": true
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | boolean | 固定为 `true`，表示 HTTP 服务本身可用 |
| `bot_ready` | boolean | QQ 机器人是否已连接且就绪 |
| `group_configured` | boolean | 是否已确定目标群 openid |

### 错误响应

| 状态码 | detail | 触发条件 |
|---|---|---|
| 403 | `仅允许本机访问` | 非回环来源或带转发头 |

### 注意

- `ok: true` 只代表 HTTP 服务在跑，不代表能发消息；判断可用性请同时看 `bot_ready` 和 `group_configured`。
- 该接口不发起任何 QQ 侧请求，开销可忽略，可放心高频轮询。

---

## 其他

- FastAPI 自动文档（`/docs`、`/redoc`、`/openapi.json`）默认关闭：NoneBot 驱动只有在配置了 `FASTAPI_DOCS_URL` / `FASTAPI_REDOC_URL` / `FASTAPI_OPENAPI_URL` 时才会注册。
- 机器人侧的入站能力（QQ 群命令、AI 对话等）不经过这些 HTTP 接口，见 `README.md` 与 `AGENTS.md`。