"""媒体链路的单元测试（stdlib unittest）。

运行：python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import asyncio
import http.server
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import nonebot

nonebot.init()

from nonebot.adapters.qq import Message, MessageSegment  # noqa: E402
from nonebot.adapters.qq.message import Attachment  # noqa: E402

from src.plugins.ai import chat_log, config, media  # noqa: E402

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
)  # 只需要文件头，测试不实际解码
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _attachment(kind: str, url: str) -> Attachment:
    return Attachment(kind, {"url": url})


class DescribeTest(unittest.TestCase):
    def test_text_emoji_and_media_placeholders(self):
        message = Message(
            [
                MessageSegment.text("看这个"),
                MessageSegment.emoji("14"),
                _attachment("image", "https://example.com/a.png"),
                _attachment("video", "https://example.com/b.mp4"),
            ]
        )
        parsed = media.describe(message)
        self.assertEqual(parsed.text, "看这个[表情:微笑][图片][视频]")
        self.assertEqual(
            [ref.kind for ref in parsed.attachments], ["image", "video"]
        )

    def test_mention_user_uses_name_and_stable_id(self):
        message = Message(
            [
                MessageSegment.mention_user("ABCDEF123456", "小明"),
                MessageSegment.text(" 今晚打本吗"),
            ]
        )
        self.assertEqual(media.describe(message).text, "@小明#123456 今晚打本吗")

    def test_mention_user_without_username_falls_back(self):
        message = Message([MessageSegment.mention_user("ABCDEF123456")])
        self.assertEqual(media.describe(message).text, "@玩家#123456")

    def test_mention_everyone(self):
        message = Message([MessageSegment.mention_everyone()])
        self.assertEqual(media.describe(message).text, "@全体成员")

    def test_mention_bot_is_dropped(self):
        segment = MessageSegment.mention_user("BOTOPENID", "SeatideBot")
        segment.data["is_bot"] = True
        message = Message([segment, MessageSegment.text(" 你好")])
        self.assertEqual(media.describe(message).text, "你好")

    def test_multiple_images_are_numbered(self):
        message = Message(
            [
                _attachment("image", "https://example.com/1.png"),
                _attachment("image", "https://example.com/2.png"),
            ]
        )
        self.assertEqual(media.describe(message).text, "[图片1][图片2]")

    def test_emoji_only_and_unknown_emoji(self):
        message = Message([MessageSegment.emoji("999")])
        parsed = media.describe(message)
        self.assertEqual(parsed.text, "[QQ表情:999]")
        self.assertEqual(parsed.attachments, ())

    def test_audio_and_file_are_placeholders_only(self):
        message = Message(
            [
                _attachment("audio", "https://example.com/a.amr"),
                _attachment("file", "https://example.com/a.zip"),
            ]
        )
        parsed = media.describe(message)
        self.assertEqual(parsed.text, "[语音][文件]")
        self.assertEqual(parsed.attachments, ())

    def test_empty_message(self):
        self.assertEqual(media.describe(Message()).text, "")


class SniffTest(unittest.TestCase):
    def test_sniffs_known_headers(self):
        self.assertEqual(media._sniff_content_type(PNG_BYTES), "image/png")
        self.assertEqual(
            media._sniff_content_type(b"GIF89a" + b"\x00" * 16), "image/gif"
        )
        self.assertEqual(
            media._sniff_content_type(MP4_BYTES), "video/mp4"
        )
        self.assertIsNone(media._sniff_content_type(b"hello world"))


def _run(coro):
    return asyncio.run(coro)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="seatide-media-"))
        self.patch_dir = mock.patch.object(media, "_DATA_DIR", self.temp)
        self.patch_root = mock.patch.object(
            media, "_MEDIA_ROOT", self.temp / "media"
        )
        self.patch_dir.start()
        self.patch_root.start()

        payloads = {
            "/a.png": (b"image/png", PNG_BYTES),
            "/b.mp4": (b"video/mp4", MP4_BYTES),
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                content_type, body = payloads.get(
                    self.path, (b"text/plain", b"missing")
                )
                self.send_response(200 if self.path in payloads else 404)
                self.send_header("Content-Type", content_type.decode())
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), Handler
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.patch_dir.stop()
        self.patch_root.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_download_image_and_video(self):
        items = _run(
            media.download_all(
                "group-1",
                "msg-1",
                (
                    media.AttachmentRef("image", f"{self.base}/a.png"),
                    media.AttachmentRef("video", f"{self.base}/b.mp4"),
                ),
            )
        )
        self.assertEqual([item.kind for item in items], ["image", "video"])
        for item in items:
            path = self.temp / item.path
            self.assertTrue(path.is_file(), item.path)
            self.assertTrue(item.path.startswith("media/group-1/msg-1/"))

    def test_failed_download_is_skipped(self):
        items = _run(
            media.download_all(
                "group-1",
                "msg-2",
                (
                    media.AttachmentRef("image", f"{self.base}/missing"),
                    media.AttachmentRef("image", f"{self.base}/a.png"),
                ),
            )
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "image")

    @unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
    def test_animated_gif_becomes_video(self):
        gif = self.temp / "anim.gif"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=32x32:rate=5",
                "-t",
                "0.4",
                str(gif),
            ],
            check=True,
        )
        payload = gif.read_bytes()

        async def fake_fetch(url: str):
            return payload, "image/gif"

        folder = self.temp / "media" / "group-3" / "msg-3"
        with mock.patch.object(media, "_fetch", fake_fetch):
            item = _run(
                media._download_one(
                    media.AttachmentRef(
                        "image", "https://example.com/x.gif"
                    ),
                    folder,
                    0,
                )
            )
        self.assertIsNotNone(item)
        self.assertEqual(item.kind, "video")
        self.assertEqual(item.content_type, "video/mp4")
        self.assertEqual(Path(item.path).suffix, ".mp4")
        self.assertEqual(
            [path.suffix for path in folder.iterdir()], [".mp4"]
        )


class BuildPartsTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="seatide-parts-"))
        self.patch_dir = mock.patch.object(media, "_DATA_DIR", self.temp)
        self.patch_dir.start()

    def tearDown(self):
        self.patch_dir.stop()
        shutil.rmtree(self.temp, ignore_errors=True)

    def _item(self, name: str, data: bytes, kind: str, ct: str):
        path = self.temp / "media" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return media.MediaItem(
            kind=kind,
            path=str(path.relative_to(self.temp)),
            content_type=ct,
            size=len(data),
        )

    def test_image_part_is_data_uri(self):
        item = self._item("a.png", PNG_BYTES, "image", "image/png")
        parts, notes, used = _run(media.build_parts([item], 5))
        self.assertEqual(notes, [])
        self.assertEqual(parts[0]["type"], "image_url")
        self.assertTrue(
            parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
        )
        self.assertGreater(used, 0)

    def test_video_part_is_data_uri(self):
        item = self._item("b.mp4", MP4_BYTES, "video", "video/mp4")
        parts, _, _ = _run(media.build_parts([item], 5))
        self.assertEqual(parts[0]["type"], "video_url")
        self.assertTrue(
            parts[0]["video_url"]["url"].startswith("data:video/mp4;base64,")
        )

    def test_limit_and_oversize_notes(self):
        first = self._item("a.png", PNG_BYTES, "image", "image/png")
        second = self._item("b.png", PNG_BYTES, "image", "image/png")
        parts, notes, _ = _run(media.build_parts([first, second], 1))
        self.assertEqual(len(parts), 1)
        self.assertTrue(notes)

        with mock.patch.object(config, "AI_MEDIA_MAX_IMAGE_BYTES", 4):
            parts, notes, _ = _run(media.build_parts([first], 5))
        self.assertEqual(parts, [])
        self.assertTrue(any("未附带" in note for note in notes))

    def test_video_base64_limit(self):
        item = self._item("b.mp4", MP4_BYTES, "video", "video/mp4")
        with mock.patch.object(
            config, "AI_MEDIA_MAX_VIDEO_BASE64_BYTES", 10
        ):
            parts, notes, _ = _run(media.build_parts([item], 5))
        self.assertEqual(parts, [])
        self.assertTrue(any("base64" in note for note in notes))

    def test_byte_budget_shared(self):
        first = self._item("a.png", PNG_BYTES, "image", "image/png")
        second = self._item("b.png", PNG_BYTES, "image", "image/png")
        encoded = media._base64_length(len(PNG_BYTES))
        parts, notes, used = _run(
            media.build_parts([first, second], 5, encoded + 1)
        )
        self.assertEqual(len(parts), 1)
        self.assertEqual(used, encoded)
        self.assertTrue(any("总大小" in note for note in notes))

    def test_gif_is_not_sent_unless_converted(self):
        item = self._item("a.gif", b"GIF89a" + b"\x00" * 16, "image", "image/gif")
        parts, notes, _ = _run(media.build_parts([item], 5))
        self.assertEqual(parts, [])
        self.assertTrue(any("GIF" in note for note in notes))

    def test_missing_file_note(self):
        item = media.MediaItem(
            kind="image",
            path="media/none/none/0-x.png",
            content_type="image/png",
            size=10,
        )
        parts, notes, _ = _run(media.build_parts([item], 5))
        self.assertEqual(parts, [])
        self.assertTrue(notes)


class StripPartsTest(unittest.TestCase):
    def test_strip_keeps_text(self):
        messages = [
            {"role": "system", "content": "sys"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,AA=="},
                    },
                ],
            },
        ]
        self.assertTrue(media.has_media_parts(messages))
        stripped = media.strip_media_parts(messages)
        self.assertEqual(stripped[0]["content"], "sys")
        self.assertEqual(stripped[1]["content"], "看看")
        self.assertFalse(media.has_media_parts(stripped))


class ChatLogMediaTest(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="seatide-chatlog-"))
        self.db = self.temp / "chat_log.db"
        self.patch_path = mock.patch.object(chat_log, "_DB_PATH", self.db)
        self.patch_path.start()
        chat_log._ready = False

    def tearDown(self):
        self.patch_path.stop()
        chat_log._ready = False
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_old_database_gets_media_column(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(
            """
            CREATE TABLE chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_openid TEXT NOT NULL,
                role TEXT NOT NULL,
                user_openid TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                message_id TEXT,
                created_at REAL NOT NULL
            );
            INSERT INTO chat_log
                (group_openid, role, user_openid, username, content,
                 message_id, created_at)
            VALUES ('g', 'user', 'u', '小明', '旧消息', 'm-old', 1.0);
            """
        )
        conn.commit()
        conn.close()

        entries = _run(chat_log.recent_entries("g", 10))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].content, "旧消息")
        self.assertEqual(entries[0].media, ())

    def test_media_roundtrip(self):
        item = media.MediaItem(
            kind="image",
            path="media/g/m/0-x.png",
            content_type="image/png",
            size=12,
        )
        _run(
            chat_log.record_user(
                "g",
                "u",
                "[图片]",
                message_id="m1",
                username="小明",
                media=[item],
            )
        )
        entries = _run(chat_log.recent_entries("g", 10))
        self.assertEqual(entries[0].content, "[图片]")
        self.assertEqual(entries[0].media, (item,))

        fetched = _run(chat_log.entry_for_message("g", "m1"))
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.media, (item,))
        self.assertTrue(_run(chat_log.has_message("g", "m1")))
        self.assertFalse(_run(chat_log.has_message("g", "m2")))


if __name__ == "__main__":
    unittest.main()