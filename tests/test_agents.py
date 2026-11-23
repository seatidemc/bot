"""agent 上下文组装的单元测试（stdlib unittest）。

运行：PYTHONPATH=. python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import asyncio
import time
import unittest
from unittest import mock

import nonebot

nonebot.init()

from src.plugins.ai import agents, api, chat_log, config  # noqa: E402

TIME_PATTERN = (
    r"^# 当前时间\n\n"
    r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4} 星期[一二三四五六日]$"
)


def _entry(openid: str, name: str, content: str, at: float | None = None):
    return chat_log.ChatLogEntry(
        role="user",
        user_openid=openid,
        username=name,
        content=content,
        created_at=at if at is not None else time.time(),
    )


def _agent(system_prompt: str = "") -> agents.Agent:
    return agents.Agent(
        name="test",
        model=config.ModelConfig(
            component="test",
            api_key="",
            base_url="https://example.com",
            model="test-model",
            temperature=0.0,
            max_tokens=100,
            thinking_effort="low",
            timeout=1.0,
            system_prompt=system_prompt,
            prompt_file="",
        ),
        tools=(),
    )


def _run_agent(agent, current, history):
    captured: dict[str, list] = {}

    async def fake_chat_completion(model, messages, **kwargs):
        captured["messages"] = messages
        return api.Completion(
            content="好的", tool_calls=[], raw_message={"content": "好的"}
        )

    with mock.patch.object(api, "chat_completion", fake_chat_completion):
        reply = asyncio.run(agents.run_agent(agent, current, history))
    return reply, captured["messages"]


class RunAgentTest(unittest.TestCase):
    def test_current_time_is_always_the_first_context_message(self):
        current = _entry("ABCDEF123456", "小明", "现在几点")
        reply, messages = _run_agent(_agent(), current, [])
        self.assertEqual(reply, "好的")
        self.assertEqual([m["role"] for m in messages], ["user", "user"])
        self.assertRegex(messages[0]["content"], TIME_PATTERN)
        self.assertIn("# 当前消息", messages[1]["content"])

    def test_time_precedes_history_and_current_message(self):
        now = time.time()
        history = [
            _entry("AAAAAA111111", "小红", "早上好", now - 3600),
            _entry("BBBBBB222222", "小刚", "中午吃啥", now - 600),
        ]
        current = _entry("ABCDEF123456", "小明", "下午打本吗", now)
        _, messages = _run_agent(_agent(), current, history)
        self.assertEqual([m["role"] for m in messages], ["user"] * 3)
        self.assertRegex(messages[0]["content"], TIME_PATTERN)
        self.assertIn("# 最近聊天记录", messages[1]["content"])
        self.assertIn("小红#111111", messages[1]["content"])
        self.assertIn("# 当前消息", messages[2]["content"])

    def test_system_prompt_stays_first(self):
        _, messages = _run_agent(
            _agent(system_prompt="你是群机器人"),
            _entry("ABCDEF123456", "小明", "你好"),
            [],
        )
        self.assertEqual(
            [m["role"] for m in messages], ["system", "user", "user"]
        )
        self.assertRegex(messages[1]["content"], TIME_PATTERN)


if __name__ == "__main__":
    unittest.main()