"""群消息里的图片 / 视频 / QQ 表情。

QQ 群消息的图片和视频以 ``Attachment`` 段到达（``seg.type`` 为 ``image`` /
``video``），URL 带签名、会过期，所以收到消息后立即下载到 ``data/media``，
聊天记录只保存本地相对路径。调用模型时再把本地文件转成 openai-completions
的多模态 content part：

- 图片：``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``
- 视频：``{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}``
  （Qwen / DashScope compatible-mode 的写法；GIF 转出的 MP4 也走这里）

QQ 内置表情（小黄脸）不是附件，只存在于 content 的 ``<emoji:id>`` 或
``<faceType=...,faceId="...">`` 里，无法下载；这里转成 ``[表情:微笑]``
这样的文本，让模型能理解玩家发的是什么表情。表情包（收藏的图片表情）
走图片附件，按图片处理。

GIF 严格来说是图片格式：单帧 GIF 按图片发送；动图（多帧）在
``AI_MEDIA_GIF_TO_VIDEO`` 开启且 ffmpeg 可用时转成 MP4 按视频发送。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.parse import urlparse

import httpx
from nonebot.log import logger

from . import config

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DATA_DIR = (_PROJECT_ROOT / "data").resolve()
_MEDIA_ROOT = _DATA_DIR / "media"

_CHUNK_SIZE = 256 * 1024
_MAX_CONCURRENT_DOWNLOADS = 3

# 百炼视频理解要求视频时长至少 2 秒（Qwen3.8 系列为 2 秒 ~ 2 小时），
# GIF 转出的 MP4 往往不足 2 秒，循环补齐后再交给模型。
_MIN_VIDEO_SECONDS = 2.0

# 可下载、可交给模型的附件类型；audio / file 只保留文本标记
_DOWNLOADABLE_KINDS = {"image", "video"}
_KIND_LABELS = {"image": "图片", "video": "视频"}

# 与 chat_log.stable_id 保持一致（media 被 chat_log 依赖，不能反向导入）
_STABLE_ID_LEN = 6

# QQ 内置小黄脸 id → 名称（经典 0~103；未知 id 显示成 [QQ表情:id]）
QQ_EMOJI_NAMES: dict[str, str] = {
    "0": "惊讶", "1": "撇嘴", "2": "色", "3": "发呆", "4": "得意",
    "5": "流泪", "6": "害羞", "7": "闭嘴", "8": "睡", "9": "大哭",
    "10": "尴尬", "11": "发怒", "12": "调皮", "13": "呲牙", "14": "微笑",
    "15": "难过", "16": "酷", "17": "抓狂", "18": "吐", "19": "偷笑",
    "20": "可爱", "21": "白眼", "22": "傲慢", "23": "饥饿", "24": "困",
    "25": "惊恐", "26": "流汗", "27": "憨笑", "28": "悠闲", "29": "奋斗",
    "30": "咒骂", "31": "疑问", "32": "嘘", "33": "晕", "34": "折磨",
    "35": "衰", "36": "骷髅", "37": "敲打", "38": "再见", "39": "擦汗",
    "40": "抠鼻", "41": "鼓掌", "42": "糗大了", "43": "坏笑", "44": "左哼哼",
    "45": "右哼哼", "46": "哈欠", "47": "鄙视", "48": "委屈", "49": "快哭了",
    "50": "阴险", "51": "亲亲", "52": "吓", "53": "可怜", "54": "菜刀",
    "55": "西瓜", "56": "啤酒", "57": "篮球", "58": "乒乓", "59": "咖啡",
    "60": "饭", "61": "猪头", "62": "玫瑰", "63": "凋谢", "64": "示爱",
    "65": "爱心", "66": "心碎", "67": "蛋糕", "68": "闪电", "69": "炸弹",
    "70": "刀", "71": "足球", "72": "瓢虫", "73": "便便", "74": "月亮",
    "75": "太阳", "76": "礼物", "77": "拥抱", "78": "强", "79": "弱",
    "80": "握手", "81": "胜利", "82": "抱拳", "83": "勾引", "84": "拳头",
    "85": "差劲", "86": "爱你", "87": "NO", "88": "OK", "89": "爱情",
    "90": "飞吻", "91": "跳跳", "92": "发抖", "93": "怄火", "94": "转圈",
    "95": "磕头", "96": "回头", "97": "跳绳", "98": "挥手", "99": "激动",
    "100": "街舞", "101": "献吻", "102": "左太极", "103": "右太极",
}

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/heic": ".heic",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-msvideo": ".avi",
    "video/x-matroska": ".mkv",
    "video/x-flv": ".flv",
    "video/x-ms-wmv": ".wmv",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
}

_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# ─── 数据结构 ────────────────────────────────────────────────


@dataclass(frozen=True)
class AttachmentRef:
    """消息里待下载的附件；``kind`` 为 image / video。"""

    kind: str
    url: str


@dataclass(frozen=True)
class ParsedMessage:
    """一条群消息的文本表示 + 需要下载的附件。"""

    text: str
    attachments: tuple[AttachmentRef, ...]


@dataclass(frozen=True)
class MediaItem:
    """已经落盘的一份媒体文件。

    ``path`` 是相对 ``data/`` 的路径（如 ``media/<群>/<消息>/0-abc.png``），
    换机器迁移时只要 ``data/`` 一起搬走就能继续用。
    """

    kind: str
    path: str
    content_type: str
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "content_type": self.content_type,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> MediaItem | None:
        if not isinstance(raw, dict):
            return None
        kind = str(raw.get("kind") or "")
        path = str(raw.get("path") or "")
        if kind not in _KIND_LABELS or not path or ".." in path:
            return None
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(
            kind=kind,
            path=path,
            content_type=str(raw.get("content_type") or ""),
            size=max(0, size),
        )


def items_from_json(raw: str | None) -> tuple[MediaItem, ...]:
    """把聊天记录里的 media JSON 还原成 MediaItem；坏数据直接忽略。"""
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(value, list):
        return ()
    items = (MediaItem.from_dict(entry) for entry in value)
    return tuple(item for item in items if item is not None)


def items_to_json(items: Iterable[MediaItem]) -> str:
    return json.dumps(
        [item.to_dict() for item in items], ensure_ascii=False
    )


# ─── 消息解析 ────────────────────────────────────────────────


def _emoji_label(raw_id: str) -> str:
    emoji_id = raw_id.strip()
    name = QQ_EMOJI_NAMES.get(emoji_id)
    if name:
        return f"[表情:{name}]"
    return f"[QQ表情:{emoji_id}]" if emoji_id else "[表情]"


def _mention_label(user_id: str, username: Any) -> str:
    """群消息里 @ 其他成员时显示成 ``@昵称#标识``，与聊天记录的发言人格式一致。"""
    name = str(username or "").strip() or "玩家"
    suffix = user_id.strip()[-_STABLE_ID_LEN:]
    return f"@{name}#{suffix}" if suffix else f"@{name}"


def describe(message: Any) -> ParsedMessage:
    """把 QQ ``Message`` 转成可入库的文本 + 待下载附件。

    - text 段原样保留；
    - emoji 段转成 ``[表情:名称]``；
    - image / video 段插入 ``[图片]`` / ``[视频]`` 标记并收集 URL；
    - audio / file 段只留 ``[语音]`` / ``[文件]`` 标记；
    - mention_user 段转成 ``@昵称#标识``，其中 @bot 直接丢掉；
    - mention_everyone 段转成 ``@全体成员``；
    - ark / embed / markdown / reference 等没有可读正文，忽略。
    """
    segments = list(message)
    counts: dict[str, int] = defaultdict(int)
    for seg in segments:
        kind = getattr(seg, "type", "")
        if kind in ("image", "video", "audio", "file"):
            counts[kind] += 1

    parts: list[str] = []
    refs: list[AttachmentRef] = []
    seen: dict[str, int] = defaultdict(int)

    for seg in segments:
        kind = getattr(seg, "type", "")
        data = getattr(seg, "data", None) or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "emoji":
            parts.append(_emoji_label(str(data.get("id") or "")))
        elif kind == "mention_user":
            # adapter 会给段补上 username / is_bot；@bot 本身没有信息量，丢掉
            if not data.get("is_bot"):
                parts.append(
                    _mention_label(
                        str(data.get("user_id") or ""), data.get("username")
                    )
                )
        elif kind == "mention_everyone":
            parts.append("@全体成员")
        elif kind in ("image", "video", "audio", "file"):
            seen[kind] += 1
            label = {
                "image": "图片",
                "video": "视频",
                "audio": "语音",
                "file": "文件",
            }[kind]
            if counts[kind] > 1:
                label = f"{label}{seen[kind]}"
            parts.append(f"[{label}]")
            url = str(data.get("url") or "").strip()
            if kind in _DOWNLOADABLE_KINDS and url:
                refs.append(AttachmentRef(kind=kind, url=url))
        # ark、embed、markdown、reference、keyboard 等忽略

    return ParsedMessage(
        text="".join(parts).strip(),
        attachments=tuple(refs),
    )


# ─── 下载 ────────────────────────────────────────────────────


def media_root() -> Path:
    return _MEDIA_ROOT


def resolve(relative_path: str) -> Path | None:
    """把聊天记录里的相对路径还原成绝对路径；越界路径返回 None。"""
    root = _DATA_DIR.resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate


def _safe_component(value: str | None) -> str:
    raw = (value or "").strip()
    cleaned = _UNSAFE_NAME_RE.sub("_", raw)[:64].strip("._")
    if cleaned:
        return cleaned
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _safe_url(url: str) -> str:
    """日志里只保留主机和路径，去掉会过期的查询串。"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return "(无效 URL)"
    return f"{parsed.scheme}://{parsed.hostname or '?'}{parsed.path}"


def _human_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"


def _sniff_content_type(data: bytes) -> str | None:
    """按文件头识别类型，避免信任可能缺失或错误的 Content-Type。"""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        # HEIC 和 MP4 都是 ISO BMFF：用 brand 区分，避免把 HEIC 当视频
        brand = data[8:12]
        if brand in {
            b"heic", b"heix", b"hevc", b"hevx",
            b"mif1", b"msf1", b"heim", b"heis",
        }:
            return "image/heic"
        return "video/mp4"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return None


def _detect_content_type(
    data: bytes, header: str, fallback_kind: str
) -> tuple[str, str]:
    """返回（content_type, kind）；文件头优先，其次响应头，最后按附件类型。"""
    content_type = _sniff_content_type(data)
    if content_type is None and header:
        content_type = header
    if content_type is None:
        content_type = (
            "image/jpeg" if fallback_kind == "image" else "video/mp4"
        )
    if content_type.startswith("video/"):
        kind = "video"
    elif content_type.startswith("image/"):
        kind = "image"
    else:
        kind = fallback_kind
    return content_type, kind


def _extension_for(content_type: str) -> str:
    ext = _EXT_BY_CONTENT_TYPE.get(content_type)
    if ext:
        return ext
    guessed = mimetypes.guess_extension(content_type or "")
    return guessed if guessed and len(guessed) <= 6 else ".bin"


async def _fetch(url: str) -> tuple[bytes, str]:
    """下载完整附件；超过上限直接中止。返回（内容, 响应头 Content-Type）。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("附件 URL 不是 http/https 地址")

    limit = config.AI_MEDIA_MAX_DOWNLOAD_BYTES
    chunks: list[bytes] = []
    total = 0
    async with httpx.AsyncClient(
        timeout=config.AI_MEDIA_DOWNLOAD_TIMEOUT,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            header = (
                response.headers.get("content-type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        f"附件超过下载上限 {_human_bytes(limit)}"
                    )
                chunks.append(chunk)
    if total == 0:
        raise ValueError("附件内容为空")
    return b"".join(chunks), header


def _write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _kill(proc: asyncio.subprocess.Process | None) -> None:
    """等待超时后终止 ffmpeg / ffprobe，避免子进程留在后台。"""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except (ProcessLookupError, asyncio.TimeoutError):
        pass


def ffmpeg_available() -> bool:
    return shutil.which(config.AI_MEDIA_FFMPEG_PATH) is not None


def _ffprobe_path() -> str | None:
    ffmpeg = shutil.which(config.AI_MEDIA_FFMPEG_PATH)
    if ffmpeg:
        sibling = Path(ffmpeg).with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe")


async def _gif_info(path: Path) -> tuple[int | None, float | None]:
    """返回（帧数, 时长秒）；ffprobe 不可用或解析失败时为 None。"""
    probe = _ffprobe_path()
    if probe is None:
        return None, None
    args = [
        probe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=nb_read_frames:format=duration",
        "-of",
        "json",
        str(path),
    ]
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(),
            timeout=min(30.0, config.AI_MEDIA_FFMPEG_TIMEOUT),
        )
    except (OSError, asyncio.TimeoutError):
        await _kill(proc)
        return None, None

    try:
        data = json.loads(stdout.decode("utf-8", "ignore"))
        streams = data.get("streams") or []
        frames_raw = streams[0].get("nb_read_frames") if streams else None
        duration_raw = (data.get("format") or {}).get("duration")
        frames = int(frames_raw) if frames_raw not in (None, "N/A") else None
        duration = (
            float(duration_raw)
            if duration_raw not in (None, "N/A")
            else None
        )
        return frames, duration
    except (ValueError, TypeError, AttributeError, IndexError):
        return None, None


async def _run_ffmpeg(ffmpeg: str, args: list[str]) -> tuple[bool, str]:
    """执行一次 ffmpeg；返回（是否成功, 失败摘要）。"""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=config.AI_MEDIA_FFMPEG_TIMEOUT,
        )
    except (OSError, asyncio.TimeoutError) as e:
        await _kill(proc)
        return False, e.__class__.__name__

    if proc.returncode != 0:
        return False, stderr.decode("utf-8", "ignore").strip()[-200:]
    return True, ""


def _h264_args() -> list[str]:
    return [
        "-an",
        "-movflags",
        "+faststart",
        "-pix_fmt",
        "yuv420p",
        # H.264 要求宽高为偶数
        "-vf",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2",
    ]


async def _convert_gif(
    source: Path,
) -> tuple[str, Path, str, int] | None:
    """把 GIF 转成百炼支持的格式。

    百炼图像支持的格式里没有 GIF（BMP/JPEG/PNG/TIFF/WEBP/HEIC），所以：

    - 单帧 GIF → PNG，按图片发送；
    - 动图 → MP4，按视频发送；不足 2 秒时循环补齐（文档要求视频
      时长至少 2 秒）。

    ffmpeg 不可用或转换失败时返回 None，保留原 GIF 文件。
    """
    ffmpeg = shutil.which(config.AI_MEDIA_FFMPEG_PATH)
    if ffmpeg is None:
        return None

    frames, duration = await _gif_info(source)

    if frames is not None and frames <= 1:
        target = source.with_suffix(".png")
        ok, detail = await _run_ffmpeg(
            ffmpeg,
            [
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(source),
                "-frames:v",
                "1",
                str(target),
            ],
        )
        if not ok or not target.is_file():
            logger.warning(f"GIF 转图片失败：{detail}")
            _unlink(target)
            return None
        size = target.stat().st_size
        if size <= 0:
            _unlink(target)
            return None
        _unlink(source)
        return "image", target, "image/png", size

    target = source.with_suffix(".mp4")
    args = ["-y", "-hide_banner", "-loglevel", "error"]
    if duration is not None and duration < _MIN_VIDEO_SECONDS:
        args += ["-stream_loop", "-1", "-i", str(source)]
        args += ["-t", f"{_MIN_VIDEO_SECONDS:g}"]
    else:
        args += ["-i", str(source)]
    args += _h264_args()
    args.append(str(target))

    ok, detail = await _run_ffmpeg(ffmpeg, args)
    if not ok or not target.is_file():
        logger.warning(f"GIF 转视频失败：{detail}")
        _unlink(target)
        return None

    size = target.stat().st_size
    if size <= 0:
        _unlink(target)
        return None
    _unlink(source)
    return "video", target, "video/mp4", size


async def _download_one(
    ref: AttachmentRef, folder: Path, index: int
) -> MediaItem | None:
    data, header = await _fetch(ref.url)
    content_type, kind = _detect_content_type(data, header, ref.kind)
    digest = hashlib.sha256(data).hexdigest()[:12]
    ext = _extension_for(content_type)
    target = folder / f"{index}-{digest}{ext}"
    await asyncio.to_thread(_write_file, target, data)

    item = MediaItem(
        kind=kind,
        path=target.relative_to(_DATA_DIR).as_posix(),
        content_type=content_type,
        size=len(data),
    )
    logger.info(
        f"媒体已保存：{item.path}（{_human_bytes(item.size)}，{content_type}）"
    )

    if content_type == "image/gif" and config.AI_MEDIA_GIF_TO_VIDEO:
        converted = await _convert_gif(target)
        if converted is not None:
            new_kind, new_path, new_type, size = converted
            item = MediaItem(
                kind=new_kind,
                path=new_path.relative_to(_DATA_DIR).as_posix(),
                content_type=new_type,
                size=size,
            )
            logger.info(
                f"GIF 已转为{'视频' if new_kind == 'video' else '图片'}："
                f"{item.path}（{_human_bytes(size)}，{new_type}）"
            )
    return item


async def download_all(
    group_openid: str,
    message_key: str | None,
    attachments: Sequence[AttachmentRef],
) -> list[MediaItem]:
    """并发下载一条消息里的全部图片 / 视频；单个失败只跳过该文件。"""
    targets = [
        ref
        for ref in attachments
        if ref.kind in _DOWNLOADABLE_KINDS and ref.url
    ]
    if not targets:
        return []

    key = _safe_component(message_key) if message_key else (
        hashlib.sha256(str([r.url for r in targets]).encode()).hexdigest()[:16]
    )
    folder = _MEDIA_ROOT / _safe_component(group_openid) / key

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

    async def run(index: int, ref: AttachmentRef) -> MediaItem | None:
        async with semaphore:
            return await _download_one(ref, folder, index)

    results = await asyncio.gather(
        *(run(index, ref) for index, ref in enumerate(targets)),
        return_exceptions=True,
    )

    items: list[MediaItem] = []
    for ref, result in zip(targets, results):
        if isinstance(result, BaseException):
            # 签名 URL 不能进日志：只保留主机 + 路径，异常文本里的 URL
            # （httpx 会带上完整查询串）也替换成状态码 / 类型名
            if isinstance(result, httpx.HTTPStatusError):
                detail = f"HTTP {result.response.status_code}"
            else:
                detail = f"{result.__class__.__name__}: {result}"
            logger.warning(
                f"媒体下载失败（{ref.kind}）：{_safe_url(ref.url)} {detail}"
            )
        elif result is not None:
            items.append(result)
    return items


# ─── 模型 content part ──────────────────────────────────────


def _read_base64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _base64_length(size: int) -> int:
    """文件转成 base64 后的字符数（不含 data: 前缀）。"""
    return ((max(0, size) + 2) // 3) * 4


async def build_parts(
    items: Sequence[MediaItem],
    limit: int,
    byte_budget: int | None = None,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """把媒体文件转成 content part。

    返回（parts, 给模型的省略说明, 已用 base64 字符数）。限制来自百炼
    文档：

    - 图片：编码前不超过 ``AI_MEDIA_MAX_IMAGE_BYTES``（Qwen3.8 的 Base64
      Data URI 上限为 20MB）；
    - 视频：编码后的 base64 字符串必须小于 10MB，由
      ``AI_MEDIA_MAX_VIDEO_BASE64_BYTES`` 控制；
    - 整次请求的媒体 base64 总量不超过 ``byte_budget``（DashScope 请求体
      上限 16 MiB，默认预算 12 MiB）；
    - ``image/gif`` 不在百炼支持的图片格式内，未成功转码时不附带。

    ``limit`` 是本次请求最多附带多少个媒体（按传入顺序取前几个）。
    """
    if limit <= 0 or not items:
        return [], [], 0

    parts: list[dict[str, Any]] = []
    notes: list[str] = []
    used = 0

    for index, item in enumerate(items):
        if len(parts) >= limit:
            notes.append(
                f"另有 {len(items) - index} 个媒体未附带"
                f"（单次最多 {limit} 个）"
            )
            break

        path = resolve(item.path)
        if path is None or not path.is_file():
            logger.warning(f"媒体文件缺失，跳过：{item.path}")
            notes.append(f"{_KIND_LABELS[item.kind]}文件已丢失，未附带")
            continue

        if item.content_type == "image/gif":
            # 百炼支持的图片格式不含 GIF；正常的 GIF 已转成 PNG / MP4，
            # 走到这里说明关闭了转码或 ffmpeg 转换失败
            logger.info(f"GIF 未转换为支持的格式，跳过：{item.path}")
            notes.append("GIF 未能转换为支持的格式，未附带")
            continue

        size = path.stat().st_size
        if item.kind == "image":
            if size > config.AI_MEDIA_MAX_IMAGE_BYTES:
                logger.info(
                    f"图片超过模型附带上限，跳过：{item.path}"
                    f"（{_human_bytes(size)} > "
                    f"{_human_bytes(config.AI_MEDIA_MAX_IMAGE_BYTES)}）"
                )
                notes.append(
                    "图片超过 "
                    f"{_human_bytes(config.AI_MEDIA_MAX_IMAGE_BYTES)}，"
                    "未附带"
                )
                continue

        encoded_len = _base64_length(size)
        if (
            item.kind == "video"
            and encoded_len > config.AI_MEDIA_MAX_VIDEO_BASE64_BYTES
        ):
            logger.info(
                f"视频 base64 后超过上限，跳过：{item.path}"
                f"（{encoded_len} > "
                f"{config.AI_MEDIA_MAX_VIDEO_BASE64_BYTES}）"
            )
            notes.append(
                "视频 base64 编码后超过 "
                f"{_human_bytes(config.AI_MEDIA_MAX_VIDEO_BASE64_BYTES)}，"
                "未附带"
            )
            continue
        if byte_budget is not None and used + encoded_len > byte_budget:
            notes.append(
                "媒体总大小超过单次请求上限"
                f"（{_human_bytes(byte_budget)}），未附带"
            )
            continue

        try:
            encoded = await asyncio.to_thread(_read_base64, path)
        except OSError as e:
            logger.warning(f"媒体读取失败，跳过：{item.path}（{e}）")
            notes.append(f"{_KIND_LABELS[item.kind]}读取失败，未附带")
            continue

        used += len(encoded)
        uri = f"data:{item.content_type or 'application/octet-stream'};base64,{encoded}"
        if item.kind == "video":
            parts.append({"type": "video_url", "video_url": {"url": uri}})
        else:
            parts.append({"type": "image_url", "image_url": {"url": uri}})

    # 同一个原因可能出现多次，说明按首次出现顺序去重
    notes = list(dict.fromkeys(notes))
    return parts, notes, used


_MEDIA_PART_TYPES = ("image_url", "video_url")


def has_media_parts(messages: Sequence[dict[str, Any]]) -> bool:
    for message in messages:
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(part, dict) and part.get("type") in _MEDIA_PART_TYPES
            for part in content
        ):
            return True
    return False


def strip_media_parts(
    messages: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """去掉全部图片 / 视频 part，只保留文本块（模型不支持多模态时的兜底）。"""
    stripped: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
            stripped.append({**message, "content": text})
        else:
            stripped.append(message)
    return stripped