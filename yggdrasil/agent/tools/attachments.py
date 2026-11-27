"""Tools that read KOOK attachments: ``read_file`` and ``view_image``."""

import asyncio
from typing import Any

from pydantic_ai import BinaryContent, RunContext, ToolReturn

from yggdrasil.agent.deps import Deps, Handle
from yggdrasil.agent.media import ModelImage, is_image, load_image, media_bytes, media_type, model_image
from yggdrasil.agent.tools.base import ToolResult, chat_tool, resolve_ref, vision
from yggdrasil.llm.registry import TaskNotConfigured, sees_images
from yggdrasil.tools.errors import ToolError


@chat_tool("read_file")
async def read_file(ctx: RunContext[Deps], handle: str) -> ToolResult:
    """读取文件内容（PDF/Office/文本/代码等），参数是 file#N 句柄，或聊天记录里的消息编号 #N（支持同服务器其他频道）。
    扫描版 PDF：能看图的模型直接收到页面图片。图片文件按看图处理。"""
    from yggdrasil.tools.docs import parse_document

    h = await resolve_ref(ctx, handle, "file")
    deps, cfg = ctx.deps, ctx.deps.settings.tools
    blob, name = await media_bytes(h, cfg.file_max_mb * 1024 * 1024, made=deps.made_images)
    if is_image(blob):
        return await _show_image(ctx, handle, h, await asyncio.to_thread(model_image, blob), "")
    doc = await asyncio.to_thread(parse_document, blob, name, max_pages=cfg.file_max_pages)
    note = "（内容已截断）" if doc.truncated else ""
    header = f"文件：{name}（{doc.kind}，{doc.pages} 页）{note}"
    if doc.scanned_pages and _hands_images(deps):
        pages: list[Any] = []
        for i, page in enumerate(doc.scanned_pages, 1):
            pages += [f"第 {i} 页：", BinaryContent(data=page, media_type=media_type(page))]
        return ToolReturn(
            return_value=f"{header}\n\n扫描版，没有文字层；前 {len(doc.scanned_pages)} 页的图片附在下一条消息里，"
            "直接看图。",
            content=pages,
        )
    text = doc.text
    if doc.scanned_pages:
        pages = [ModelImage(page) for page in doc.scanned_pages]
        text = await vision(ctx, pages, "这是扫描版文档的页面，请按顺序完整转写文字，保留结构。")
    return f"{header}\n\n{text}"


@chat_tool("view_image")
async def view_image(ctx: RunContext[Deps], handle: str, question: str = "") -> ToolResult:
    """查看图片，可附带具体问题。handle 是 img#N 句柄、图片格式的 file#N，或聊天记录里的消息编号 #N
    （支持同服务器其他频道，多张时用 #N/2 取第 2 张）。能看图的模型直接收到原图，否则返回文字描述。"""
    h = await resolve_ref(ctx, handle, "image", "file")
    img = await load_image(h, made=ctx.deps.made_images)
    return await _show_image(ctx, handle, h, img, question)


@chat_tool("check_provenance")
async def check_provenance(ctx: RunContext[Deps], handle: str) -> str:
    """用 OpenAI Content Provenance API（/v1/content_provenance_checks）检测图片、音频或 PDF 是否包含
    OpenAI 溯源信号（C2PA 内容凭证、SynthID 水印）。handle 是 img#N、file#N，或聊天记录里的消息编号 #N
    （多张时用 #N/2）。会直接上传原始文件字节，不做重压缩。"""
    from yggdrasil.tools.provenance import (
        PROVENANCE_MAX_BYTES,
        ProvenanceError,
        check_content_provenance,
        format_provenance_report,
    )

    h = await resolve_ref(ctx, handle, "image", "file")
    deps, cfg = ctx.deps, ctx.deps.settings.tools
    max_bytes = min(cfg.file_max_mb * 1024 * 1024, PROVENANCE_MAX_BYTES)
    blob, name = await media_bytes(h, max_bytes, made=deps.made_images)
    mime = media_type(blob) if is_image(blob) else ""
    try:
        report = await check_content_provenance(
            blob,
            name or "file.bin",
            mime=mime,
            provider_ref=cfg.provenance_provider,
        )
    except ProvenanceError as e:
        raise ToolError(str(e)) from e
    return format_provenance_report(report, filename=name)


def _hands_images(deps: Deps) -> bool:
    return not deps.text_only and sees_images(deps.task)


async def _show_image(ctx: RunContext[Deps], ref: str, h: Handle, img: ModelImage, question: str) -> ToolResult:
    ctx.deps.notes.image(img.data, img.content.media_type, segment=h.data)
    if _hands_images(ctx.deps):
        return ToolReturn(
            return_value=f"{ref} 的原图附在下一条消息里，直接看图。",
            content=[f"{ref}{img.note}：", img.content],
        )
    try:
        return "（以下是另一个模型写的图片描述）\n" + await vision(ctx, [img], question)
    except TaskNotConfigured as e:
        raise ToolError("没有配置能看图的模型，看不了这张图") from e
