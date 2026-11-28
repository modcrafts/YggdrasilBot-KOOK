"""Downloading and preparing KOOK message attachments (images, files) for the model."""

import asyncio
import io
import itertools
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath, PurePosixPath
from typing import Any

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageSequence, ImageStat
from pydantic_ai import BinaryContent

from yggdrasil.agent.deps import Handle
from yggdrasil.core.log import LOGGER
from yggdrasil.tools.errors import ToolError
from yggdrasil.tools.ssrf import guarded_client
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.store import blob as stored_blob

IMAGE_MAX_DOWNLOAD = 20 * 1024 * 1024
_IMAGE_MAX_SIDE = 1568
_IMAGE_MAX_BYTES = 3 * 1024 * 1024
_CONTEXT_IMAGE_LIMIT = 12
_CONTEXT_IMAGE_TIMEOUT_S = 5
_CONTEXT_IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")

_SHEET_MAX_FRAMES = 16
_SAMPLE_STEP_MS = 100
_HOLD_MS = 300
_CHANGE = 0.06
_MIN_CHANGE = 0.015
_MAX_DECODE_FRAMES = 600
_DEFAULT_FRAME_MS = 100
_SHEET_GAP = 4
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_UNSAFE_NAME = re.compile(r"[^\w.\-]+")


class MediaTooLarge(ToolError):
    """An attachment is larger than the caller allows."""


async def download(url: str, max_bytes: int, *, guarded: bool = False) -> bytes:
    client = guarded_client(timeout=60) if guarded else httpx.AsyncClient(timeout=60, follow_redirects=True)
    async with client, client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise MediaTooLarge(f"文件太大（超过 {max_bytes // 1024 // 1024}MB）")
            chunks.append(chunk)
    return b"".join(chunks)


async def media_bytes(h: Handle, max_bytes: int, *, made: Mapping[str, bytes] | None = None) -> tuple[bytes, str]:
    """Bytes + file name of an image/file handle: in-memory ``made``, stored ``transcript_blobs``, else URL."""
    data = h.data
    name = str(data.get("file_name") or data.get("name") or "file")
    if sha := data.get("blob"):
        if made and (own := made.get(str(sha))) is not None:
            return own, name
        if (kept := await stored_blob(str(sha))) is not None:
            return kept.data, name
        if data.get("made"):
            raise ToolError(f"{h.key} 是之前生成的图，已经找不到了；需要的话重新生成")
    url = str(data.get("url") or "")
    if not url:
        what = "图片" if h.kind == "image" else "文件"
        raise ToolError(f"拿不到{what}的下载地址")
    guarded = bool(data.get("source")) and data.get("source") != "kook"
    try:
        return await download(url, max_bytes, guarded=guarded), name
    except httpx.HTTPStatusError as e:
        what = "图片" if h.kind == "image" else "文件"
        raise ToolError(f"下载{what}失败（HTTP {e.response.status_code}）") from e


def is_image(blob: bytes) -> bool:
    try:
        Image.open(io.BytesIO(blob)).close()
    except Image.UnidentifiedImageError:
        return False
    except (OSError, ValueError, Image.DecompressionBombError):
        return True
    return True


def looks_like_image(h: Handle) -> bool:
    if h.kind == "image":
        return True
    name = str(h.data.get("file_name") or h.data.get("name") or "")
    return h.kind == "file" and PurePath(name).suffix.lower() in _IMAGE_SUFFIXES


def media_type(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG"):
        return "image/png"
    if blob[:3] == b"GIF":
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def safe_name(name: str, fallback: str) -> str:
    base = _UNSAFE_NAME.sub("_", PurePosixPath(name.replace("\\", "/")).name).strip("._")[:120]
    return base or fallback


def file_name(ref: str, h: Handle, blob: bytes) -> str:
    fallback = re.sub(r"\W+", "-", ref).strip("-") or "file"
    if is_image(blob):
        fallback += "." + media_type(blob).removeprefix("image/").replace("jpeg", "jpg")
    return safe_name(str(h.data.get("file_name") or h.data.get("name") or ""), fallback)


def fit_image(blob: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(blob))
        too_big = max(img.size) > _IMAGE_MAX_SIDE or len(blob) > _IMAGE_MAX_BYTES
        if not too_big and img.format in ("PNG", "JPEG", "GIF", "WEBP"):
            return blob
        img.thumbnail((_IMAGE_MAX_SIDE, _IMAGE_MAX_SIDE))
        out = io.BytesIO()
        if img.mode in ("RGBA", "LA", "P"):
            img.convert("RGBA").save(out, "PNG", optimize=True)
        else:
            img.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()
    except (OSError, ValueError, Image.DecompressionBombError):
        return blob


_SEND_MAX_BYTES = 1024 * 1024


def compact_image(blob: bytes) -> bytes:
    """Re-encode large opaque images as JPEG for faster KOOK asset upload; keep animations & alpha intact."""
    if len(blob) <= _SEND_MAX_BYTES:
        return blob
    try:
        with Image.open(io.BytesIO(blob)) as img:
            if getattr(img, "is_animated", False):
                return blob
            if img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info:
                lowest_alpha = img.convert("RGBA").getchannel("A").getextrema()[0]
                if isinstance(lowest_alpha, int | float) and lowest_alpha < 255:
                    return blob
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
    except (OSError, ValueError, Image.DecompressionBombError):
        return blob
    data = out.getvalue()
    return data if len(data) < len(blob) else blob


@dataclass(frozen=True, slots=True)
class ModelImage:
    data: bytes
    times: tuple[float, ...] = ()
    duration: float = 0.0

    @property
    def note(self) -> str:
        if not self.times:
            return ""
        return (
            f"（动图，时长 {self.duration:.1f} 秒：按时间顺序抽了 {len(self.times)} 帧拼成一张，"
            "从左到右、从上到下看，每格左上角是帧号和秒数；这是同一个动画，不是多张图）"
        )

    @property
    def content(self) -> BinaryContent:
        return BinaryContent(data=self.data, media_type=media_type(self.data))


def model_image(blob: bytes) -> ModelImage:
    try:
        with Image.open(io.BytesIO(blob)) as img:
            if getattr(img, "is_animated", False) and (sheet := _frame_sheet(img)) is not None:
                return sheet
    except (OSError, ValueError, Image.DecompressionBombError):
        pass
    return ModelImage(fit_image(blob))


def _flatten(frame: Image.Image) -> Image.Image:
    rgba = frame.convert("RGBA")
    out = Image.new("RGB", rgba.size, "white")
    out.paste(rgba, mask=rgba.getchannel("A"))
    return out


def _change(a: Image.Image, b: Image.Image) -> float:
    diff = ImageChops.difference(a, b)
    cells = ((x, y, x + 16, y + 16) for x in range(0, 64, 16) for y in range(0, 64, 16))
    return max(sum(ImageStat.Stat(diff.crop(cell)).mean) / (3 * 255) for cell in cells)


type _Sample = tuple[int, int, Image.Image]


def _samples(img: Image.Image) -> tuple[list[_Sample], int]:
    samples: list[_Sample] = []
    start = sample = 0
    for i, frame in enumerate(itertools.islice(ImageSequence.Iterator(img), _MAX_DECODE_FRAMES)):
        ms = int(frame.info.get("duration") or 0)
        end = start + (ms if ms > 10 else _DEFAULT_FRAME_MS)
        if sample < end:
            samples.append((i, sample, _flatten(frame).resize((64, 64), Image.Resampling.BILINEAR)))
            sample += (end - sample + _SAMPLE_STEP_MS - 1) // _SAMPLE_STEP_MS * _SAMPLE_STEP_MS
        start = end
    return samples, start


def _changed(samples: list[_Sample]) -> list[_Sample]:
    kept = [samples[0]]
    for s in samples[1:]:
        d = _change(s[2], kept[-1][2])
        if d >= _CHANGE or (d >= _MIN_CHANGE and s[1] - kept[-1][1] >= _HOLD_MS):
            kept.append(s)
    return kept


def _frame_sheet(img: Image.Image) -> ModelImage | None:
    samples, duration = _samples(img)
    kept = _changed(samples) if samples else []
    if len(kept) < 2:
        return None
    if len(kept) > _SHEET_MAX_FRAMES:
        n = len(kept) - 1
        kept = [kept[round(k * n / (_SHEET_MAX_FRAMES - 1))] for k in range(_SHEET_MAX_FRAMES)]
    wanted = {s[0] for s in kept}
    sequence = itertools.islice(ImageSequence.Iterator(img), kept[-1][0] + 1)
    frames = [_flatten(f) for i, f in enumerate(sequence) if i in wanted]
    times = tuple(s[1] / 1000 for s in kept)
    return ModelImage(_tile(frames, times), times, duration / 1000)


def _tile(frames: list[Image.Image], times: tuple[float, ...]) -> bytes:
    w, h = frames[0].size
    n = len(frames)
    cols = max(1, min(n, round(math.sqrt(n * h / w))))
    rows = math.ceil(n / cols)
    room = _IMAGE_MAX_SIDE - _SHEET_GAP * (max(cols, rows) - 1)
    scale = min(1.0, room / (cols * w), room / (rows * h))
    cw, ch = max(1, int(w * scale)), max(1, int(h * scale))
    sheet = Image.new("RGB", (cols * cw + (cols - 1) * _SHEET_GAP, rows * ch + (rows - 1) * _SHEET_GAP), (64, 64, 64))
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=max(10, min(cw, ch) // 9))
    for k, (frame, t) in enumerate(zip(frames, times, strict=True)):
        x, y = (k % cols) * (cw + _SHEET_GAP), (k // cols) * (ch + _SHEET_GAP)
        sheet.paste(frame.resize((cw, ch), Image.Resampling.LANCZOS) if scale < 1 else frame, (x, y))
        label = f"{k + 1} {t:.1f}s"
        box = draw.textbbox((x + 3, y + 2), label, font=font)
        draw.rectangle((box[0] - 2, box[1] - 1, box[2] + 2, box[3] + 1), fill=(0, 0, 0))
        draw.text((x + 3, y + 2), label, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    sheet.save(out, "PNG", optimize=True)
    if out.tell() > _IMAGE_MAX_BYTES:
        out = io.BytesIO()
        sheet.save(out, "JPEG", quality=85)
    return out.getvalue()


async def load_image(h: Handle, *, made: Mapping[str, bytes] | None = None) -> ModelImage:
    blob, name = await media_bytes(h, IMAGE_MAX_DOWNLOAD, made=made)
    if not is_image(blob):
        raise ToolError(f"{name} 不是图片，用 read_file 读" if h.kind == "file" else f"{h.key} 不是能识别的图片格式")
    return await asyncio.to_thread(model_image, blob)


async def load_images(handles: list[Handle]) -> list[tuple[str, ModelImage]]:
    results = await asyncio.gather(*(load_image(h) for h in handles), return_exceptions=True)
    loaded = []
    for h, result in zip(handles, results, strict=True):
        if isinstance(result, BaseException):
            LOGGER.warning("inline image {} failed: {!r}", h.key, result)
        else:
            loaded.append((h.key, result))
    return loaded


async def keep_context_images(notes: TraceNotes, channel_id: str | None) -> None:
    """Retain a bounded set of raster images from this channel's request and native reply context."""
    context = notes.context
    if not notes.full or context is None:
        return

    wanted: dict[str, dict[str, Any]] = {}
    for message in (context.trigger, context.quoted, *context.chain):
        if message is None or message.channel_id != channel_id or message.recalled:
            continue
        for segment in message.segments:
            data = segment.get("data")
            if segment.get("type") != "image" or not isinstance(data, dict) or data.get("blob"):
                continue
            url = data.get("url")
            if isinstance(url, str) and url and url not in notes.image_keys:
                wanted.setdefault(url, data)
            if len(wanted) >= _CONTEXT_IMAGE_LIMIT:
                break
        if len(wanted) >= _CONTEXT_IMAGE_LIMIT:
            break

    async def keep(url: str) -> None:
        # KOOK Cards can point at arbitrary hosts, even when the segment has no source.
        image = await download(url, _IMAGE_MAX_BYTES, guarded=True)
        with Image.open(io.BytesIO(image), formats=_CONTEXT_IMAGE_FORMATS) as parsed:
            mime = parsed.get_format_mimetype()
        if mime:
            notes.image(image, mime, segment=wanted[url])

    if not wanted:
        return
    try:
        async with asyncio.timeout(_CONTEXT_IMAGE_TIMEOUT_S):
            await asyncio.gather(*(keep(url) for url in wanted), return_exceptions=True)
    except TimeoutError:
        # Successful downloads have already been added to the transcript.
        pass
