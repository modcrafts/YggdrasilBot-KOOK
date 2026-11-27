"""Images the bot creates and embeds in KOOK replies (``render_*``, workspace ``show()``, ``generate_image``)."""

import asyncio
import io
from collections import Counter
from typing import Any, Literal

from PIL import Image
from pydantic_ai import ModelRetry, RunContext

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.media import IMAGE_MAX_DOWNLOAD, compact_image, fit_image, load_image, media_bytes, media_type
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.tools.base import chat_tool, resolve_ref
from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.delivery.send import send_image
from yggdrasil.llm.registry import ImageError, generate_images
from yggdrasil.moderation.image import ImageVerdict, check_image
from yggdrasil.settings import Settings
from yggdrasil.tools import render as renderer
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.notes import blob_id

MAX_IMAGES = 4


def _verify_image(blob: bytes) -> None:
    try:
        with Image.open(io.BytesIO(blob)) as img:
            img.verify()
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ToolError("这个文件不是能发的图片（支持 PNG、JPEG、GIF、WebP 等）") from e


def _decode_image(blob: bytes) -> bytes:
    _verify_image(blob)
    return fit_image(blob)


def needs_image_check(settings: Settings) -> bool:
    return settings.moderation.enabled and settings.moderation.check_output


async def screen_image(deps: Deps, blob: bytes, *, check: Any = None) -> bytes:
    image = await asyncio.to_thread(_decode_image, blob)
    if needs_image_check(deps.settings):
        verdict: ImageVerdict = await (check or check_image)(image)
        if verdict.model:
            deps.vision_tokens.append((verdict.model, verdict.input_tokens, verdict.output_tokens))
        if not verdict.safe:
            reason = "审核没通过" if verdict.category != "error" else "审核出错"
            raise ToolError(f"这张图{reason}（{verdict.category}），不能发；不要在正文里描述它的内容")
    return image


async def attach_image(deps: Deps, blob: bytes, caption: str, *, check: Any = check_image) -> None:
    deps.images.append((await screen_image(deps, blob, check=check), caption.strip()))


def _made(deps: Deps, image: bytes, caption: str, by: Literal["render", "show"]) -> str:
    sha = blob_id(image)
    deps.made_images[sha] = image
    key = deps.handles.add("image", {"blob": sha, "caption": caption, "made": by})
    assert key is not None
    return key


def _made_text(keys: list[str], caption: str) -> str:
    refs = "、".join(keys)
    what = f"共 {len(keys)} 页，{caption or '无标题'}" if len(keys) > 1 else caption or "无标题"
    return f"已生成 {refs}（{what}）。要发就在回复某一节的 images 里写 {refs}；不引用就不会发。"


async def show_image(deps: Deps, blob: bytes, caption: str, *, call_id: str | None = None) -> str:
    await asyncio.to_thread(_verify_image, blob)
    caption = caption.strip()
    key = _made(deps, blob, caption, "show")
    deps.notes.shown(call_id, [(blob, caption)], mime=media_type(blob))
    return _made_text([key], caption)


async def _render(ctx: RunContext[Deps], kind: renderer.Kind, source: str, caption: str) -> str:
    if not APP_CONFIG.render_url:
        raise ToolError("没有配置渲染服务")
    cfg = ctx.deps.settings.tools
    wanted = cfg.render_markdown_max_pages if kind == "markdown" else 1
    pngs, warnings = await renderer.render(
        kind,
        source,
        base_url=APP_CONFIG.render_url,
        max_images=wanted,
        max_source_chars=cfg.render_max_source_chars,
        timeout_s=float(cfg.render_timeout_s),
    )
    caption = caption.strip()
    pages = [
        (png, caption if i == 1 else (f"{caption}（{i}/{len(pngs)}）" if caption else ""))
        for i, png in enumerate(pngs[:wanted], 1)
    ]
    keys = [_made(ctx.deps, png, page, "render") for png, page in pages]
    ctx.deps.notes.shown(ctx.tool_call_id, pages)
    result = _made_text(keys, caption)
    if warnings:
        lines = "\n".join(f"- {w}" for w in warnings)
        result += f"\n渲染时有问题（图已经生成，需要的话改好内容后重新生成，只引用新的那张）：\n{lines}"
    return result


@chat_tool("render_mermaid")
async def render_mermaid(ctx: RunContext[Deps], code: str, caption: str = "") -> str:
    """把 Mermaid 代码渲染成图片，返回 img#N 句柄；要发就在回复某一节的 images 里写它，不引用就不会发。"""
    return await _render(ctx, "mermaid", code, caption)


@chat_tool("render_markdown")
async def render_markdown(ctx: RunContext[Deps], markdown: str, caption: str = "") -> str:
    """把复杂 Markdown（如 GFM 表格、数学公式、多页文档）渲染成图片，返回 img#N 句柄。
    普通 Markdown 直接写在回复正文里即可。"""
    return await _render(ctx, "markdown", markdown, caption)


@chat_tool("render_html")
async def render_html(ctx: RunContext[Deps], html: str, caption: str = "") -> str:
    """把一段内联样式 HTML 渲染成图片，返回 img#N 句柄；不能用脚本或外部资源。"""
    return await _render(ctx, "html", html, caption)


def check_reply_images(ctx: RunContext[Deps], reply: BotReply) -> BotReply:
    if reply.mode != "reply":
        return reply
    deps = ctx.deps
    sections = [s.model_copy(update={"images": [k.strip() for k in s.images]}) for s in reply.sections]
    keys = [k for s in sections for k in s.images]
    for key in keys:
        h = deps.handles.get(key)
        if h is None:
            raise ModelRetry(
                f"images 里的「{key}」不是这次对话里的句柄："
                "只能写工具结果或聊天记录里出现过的 img#N，没有要发的图就留空"
            )
        if h.kind != "image":
            raise ModelRetry(f"images 里的 {key} 不是图片：只能写 img#N")
    if twice := [k for k, n in Counter(keys).items() if n > 1]:
        raise ModelRetry(f"{'、'.join(twice)} 在 images 里写了不止一次：每张图只放一次")
    room = MAX_IMAGES - len(deps.images)
    if len(keys) > room:
        attached = f"，任务的 {len(deps.images)} 张图已经附上了" if deps.images else ""
        raise ModelRetry(
            f"一条回复最多发 {MAX_IMAGES} 张图{attached}；"
            f"images 里写了 {len(keys)} 张，这次最多放 {room} 张，只留最要紧的"
        )
    return reply.model_copy(update={"sections": sections})


async def reply_images(deps: Deps, reply: BotReply) -> dict[str, tuple[bytes, str]]:
    keys = list(dict.fromkeys(k.strip() for s in reply.sections for k in s.images))
    if reply.mode != "reply" or not keys:
        return {}
    results = await asyncio.gather(*(_reply_image(deps, key) for key in keys), return_exceptions=True)
    pictures: dict[str, tuple[bytes, str]] = {}
    for key, result in zip(keys, results, strict=True):
        if isinstance(result, Exception):
            LOGGER.warning("reply image {} left out: {!r}", key, result)
        elif isinstance(result, BaseException):
            raise result
        else:
            pictures[key] = result
    return pictures


async def _reply_image(deps: Deps, key: str) -> tuple[bytes, str]:
    h = deps.handles.get(key)
    if h is None or h.kind != "image":
        raise ToolError(f"{key} 不是图片句柄")
    blob, _ = await media_bytes(h, IMAGE_MAX_DOWNLOAD, made=deps.made_images)
    made = h.data.get("made")
    image = blob if made in ("render", "sticker") else compact_image(await screen_image(deps, blob))
    return image, str(h.data.get("caption") or "") if made else ""


MAX_REFERENCES = 4
type AspectRatio = Literal["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "21:9"]


@chat_tool("generate_image")
async def generate_image(
    ctx: RunContext[Deps],
    prompt: str,
    references: list[str] | None = None,
    aspect_ratio: AspectRatio = "auto",
    text: str = "",
) -> str:
    """画图：按描述生成一张图，或参照聊天里的图改图。画好后直接发送到当前频道；没有别的要说就选 mode=done。"""
    deps = ctx.deps
    max_refs = deps.settings.tools.generate_image_max_references
    if len(references or []) > max_refs:
        raise ToolError(f"参考图最多 {max_refs} 张")
    refs: list[tuple[bytes, str]] = []
    for ref in references or []:
        h = await resolve_ref(ctx, ref, "image", "file")
        img = await load_image(h, made=deps.made_images)
        deps.notes.image(img.data, img.content.media_type, segment=h.data)
        refs.append((img.data, img.content.media_type))
    try:
        result = await generate_images(
            prompt, references=refs, aspect_ratio=None if aspect_ratio == "auto" else aspect_ratio
        )
    except ImageError as e:
        raise ToolError(f"画图失败：{e}") from e
    deps.vision_tokens.append((result.model, result.input_tokens, result.output_tokens))
    pictures = [compact_image(await screen_image(deps, data)) for data, _ in result.images]
    caption = text.strip()
    for picture in pictures:
        deps.notes.shown(ctx.tool_call_id, [(picture, caption)], mime=media_type(picture))
    cost = f"（花费 ${result.cost:.3f}）" if result.cost is not None else ""
    try:
        message_id = await send_image(
            deps.kook,
            channel_id=deps.channel_id,
            guild_id=deps.guild_id,
            user_id=deps.user_id,
            reply_to=deps.trigger_message_id,
            images=pictures,
            text=caption,
            self_id=deps.self_id,
            nickname=deps.bot_nickname,
            chat_code=deps.chat_code,
        )
    except Exception as e:
        raise ToolError(f"图画好了{cost}，但发到频道失败：{type(e).__name__}: {str(e)[:200]}") from e
    deps.actions.append(f"image {message_id or ''}".strip())
    return f"图已经发到频道里了{cost}。没有别的要说就选 mode=done。"
