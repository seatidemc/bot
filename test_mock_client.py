#!/usr/bin/env python3
"""模拟 OneBot v11 反向 WebSocket 客户端，用于本地测试 bot。

用法：
  python test_mock_client.py

前提：
  1. 后端已启动：cd ~/code/go-aliyunmc-v2 && go run . run
  2. Bot 已启动：cd ~/code/seatide2026/bot && python bot.py
  3. 有一个已绑定白名单的后端用户（用于测试 /开机）
"""

import asyncio
import json
import sys
import time
import traceback

import websockets

BOT_WS_URL = "ws://127.0.0.1:8080/onebot/v11/ws"
SELF_ID = 10001


async def send_event(ws: websockets.WebSocketClientProtocol, event: dict):
    """发送 OneBot v11 事件。"""
    await ws.send(json.dumps(event, ensure_ascii=False))
    et = event.get("post_type", "")
    mt = event.get("message_type", event.get("meta_event_type", ""))
    msg = event.get("message", "")
    print(f"  SEND: {et}.{mt} msg={msg[:60]}")


async def read_responses(
    ws: websockets.WebSocketClientProtocol, timeout: float = 2.0
):
    """读取 bot 发回的 API 调用（send_msg 等），直到超时。"""
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            break
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue

        action = msg.get("action", "")
        params = msg.get("params", {})
        echo = msg.get("echo", "")

        if action == "send_msg":
            text = params.get("message", "")
            if isinstance(text, list):
                text = "".join(
                    seg.get("data", {}).get("text", "") for seg in text
                )
            mt = params.get("message_type", "?")
            target = params.get("group_id", params.get("user_id", "?"))
            print(f"  RECV: send_msg -> {mt}({target}): {text[:200]}")

        # 回复所有 API 调用
        resp = {"status": "ok", "retcode": 0, "data": {}, "echo": echo}
        if action == "get_login_info":
            resp["data"] = {"user_id": SELF_ID, "nickname": "test_bot"}
        elif action == "send_msg":
            resp["data"] = {"message_id": int(time.time() * 1000)}
        await ws.send(json.dumps(resp))


async def main():
    print(f"Connecting to {BOT_WS_URL} ...")
    try:
        async with websockets.connect(
            BOT_WS_URL,
            ping_interval=None,
            additional_headers={"X-Self-ID": str(SELF_ID)},
        ) as ws:
            print("Connected")

            # Lifecycle
            await send_event(
                ws,
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": SELF_ID,
                    "time": int(time.time()),
                },
            )
            await asyncio.sleep(0.5)
            await read_responses(ws, timeout=1.0)

            # --- Test 1: private /login ---
            print("\n=== Test 1: private /login ===")
            TEST_QQ = 123456789
            TEST_GROUP = 123456789

            await send_event(
                ws,
                {
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "user_id": TEST_QQ,
                    "message": "/login testuser testpass",
                    "raw_message": "/login testuser testpass",
                    "self_id": SELF_ID,
                    "time": int(time.time()),
                    "message_id": 1,
                    "font": 14,
                    "sender": {"user_id": TEST_QQ, "nickname": "TestUser"},
                },
            )
            await asyncio.sleep(0.5)
            await read_responses(ws, timeout=3.0)

            # --- Test 2: group /status ---
            print("\n=== Test 2: group /status ===")
            await send_event(
                ws,
                {
                    "post_type": "message",
                    "message_type": "group",
                    "sub_type": "normal",
                    "group_id": TEST_GROUP,
                    "user_id": TEST_QQ,
                    "message": "/status",
                    "raw_message": "/status",
                    "self_id": SELF_ID,
                    "time": int(time.time()),
                    "message_id": 2,
                    "font": 14,
                    "sender": {
                        "user_id": TEST_QQ,
                        "nickname": "TestUser",
                        "card": "",
                        "role": "member",
                    },
                },
            )
            await asyncio.sleep(0.5)
            await read_responses(ws, timeout=3.0)

            print("\nDone")

    except (OSError, ConnectionRefusedError):
        print(f"ERROR: cannot connect to {BOT_WS_URL}. Is the bot running?")
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception:
        traceback.print_exc()
