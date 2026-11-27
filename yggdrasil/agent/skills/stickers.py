"""The ``stickers`` skill: on-demand lookup and selection of collected KOOK emojis and sticker images."""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability
from sqlalchemy import exists, false, or_, select

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.media import media_bytes, media_type
from yggdrasil.agent.tools.base import ToolResult, chat_tool, resolve_ref
from yggdrasil.agent.tools.images import needs_image_check
from yggdrasil.db.models import Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.kook.model import emoji_origin_guild
from yggdrasil.moderation.image import check_image
from yggdrasil.stickers.collect import MAX_BYTES, save_image_sticker
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.notes import blob_id

ID = "stickers"
DESCRIPTION = (
    "查询、选用或录入服务器表情包与自定义表情："
    "（1）聊天里出现看不懂意思的 `[表情:xxx]` / `[表情包:xxx]` 且影响理解对方意图时，"
    "可加载它查表情的画面描述、情绪和用法；"
    "（2）在轻松闲聊、玩梗、吐槽、祝贺等确实加分的场合，偶尔挑一张内联表情或动图/图片表情包配进回复或单独发图；"
    "（3）用户让你把对话里的某张图收进表情包库，或聊到非常有梗、值得收藏复用的表情图时，用 save_sticker 录入。"
    "正经问答且无需查/配/收表情包时不要加载，不要每次回复都配表情包。"
)
INSTRUCTIONS = """\
查询、选用或录入表情包库：
- 查意思：聊天里遇到看不懂的 `[表情:名称]` 且有必要弄清含义时，
  用 search_stickers("名称") 查看该表情的画面描述、图中文字、情绪和常见用法（无需调 pick_sticker）。
- 配进回复（保持克制，单次回复最多配 1 张，宁缺毋滥）：先用 search_stickers(query) 按情绪/场景搜候选，
  没搜到贴切的直接放弃；再调 pick_sticker(sticker_id)：
  - 本服自定义表情返回内联标记 `(emj)名称(emj)[id]`，直接写进回复正文即可（KMarkdown 和卡片正文都会直接显示）。
  - 图片/动图表情包返回句柄 `img#N`：放进回复某一节的 `images` 随文字一起发出；
    如果想单独发一张表情图不配文字，把该节 `content` 留空、`images` 写 `["img#N"]` 并设 `style="text"`。
- 录入表情包：用 save_sticker(handle, description, usage, emotions, ocr_text) 把对话中的图片/动图收进表情包库。
  - handle 支持 `img#N`、`file#N` 或聊天记录消息编号 `#N`（多张图用 `#N/2`）。
  - 录入前若还没看过原图，先用 view_image 看清确认确实是适合复用的表情包/梗图
    （不要录入普通截图、报错图或含隐私/违规内容的图）。
  - 顺手填好 description（一句话描述画面）、usage（适用场景）和 emotions（1-4 个情绪词）会立即生效可供检索；
    留空则交由后台视觉模型补全。"""

_POOL_LIMIT = 200
_QUERY_SPLIT = re.compile(r"[\s,，、/|]+")


@dataclass(frozen=True, slots=True)
class _StickerRow:
    id: int
    kind: str
    guild_id: str | None
    emoji_id: str | None
    summary: str
    description: str
    usage: str
    emotions: list[str]
    ocr_text: str
    frames: int | None
    uses: int
    seen_count: int


def _origin_guild(emoji_id: str | None, guild_id: str | None) -> str | None:
    return emoji_origin_guild(emoji_id) or guild_id or None


def _is_same_guild_emoji(
    kind: str, emoji_id: str | None, sticker_guild_id: str | None, target_guild_id: str | None
) -> bool:
    if kind != "emoji" or not emoji_id or not target_guild_id:
        return False
    return _origin_guild(emoji_id, sticker_guild_id) == target_guild_id


async def _load_pool(guild_id: str | None, *, limit: int = _POOL_LIMIT) -> list[_StickerRow]:
    has_blob = exists().where(StickerBlob.sticker_id == Sticker.id)
    same_guild_emoji = (
        (Sticker.kind == "emoji")
        & Sticker.emoji_id.is_not(None)
        & (Sticker.emoji_id != "")
        & (Sticker.guild_id == guild_id)
        if guild_id
        else false()
    )
    ready_or_named_emoji = or_(
        (Sticker.status == "ready") & or_(has_blob, same_guild_emoji),
        same_guild_emoji & (Sticker.summary != ""),
    )
    stmt = (
        select(Sticker, StickerUsage.count, has_blob)
        .outerjoin(
            StickerUsage,
            (StickerUsage.sticker_id == Sticker.id) & (StickerUsage.guild_id == (guild_id or "")),
        )
        .where(Sticker.enabled.is_(True), ready_or_named_emoji)
        .order_by(StickerUsage.count.desc().nullslast(), Sticker.seen_count.desc(), Sticker.id.desc())
        .limit(limit)
    )
    async with get_session() as session:
        rows = (await session.execute(stmt)).all()
    out: list[_StickerRow] = []
    for s, uses, row_has_blob in rows:
        same_guild = _is_same_guild_emoji(s.kind, s.emoji_id, s.guild_id, guild_id)
        # Never expose stale foreign-guild emojis that lack a usable image blob (or aren't ready).
        if not same_guild and (s.status != "ready" or not row_has_blob):
            continue
        out.append(
            _StickerRow(
                id=s.id,
                kind=s.kind,
                guild_id=_origin_guild(s.emoji_id, s.guild_id) if s.kind == "emoji" else s.guild_id,
                emoji_id=s.emoji_id,
                summary=s.summary or "",
                description=s.description or "",
                usage=s.usage or "",
                emotions=[str(e) for e in (s.emotions or ())],
                ocr_text=s.ocr_text or "",
                frames=s.frames,
                uses=int(uses or 0),
                seen_count=s.seen_count,
            )
        )
    return out


def _score(row: _StickerRow, terms: list[str], guild_id: str | None) -> tuple[int, int, int, int, int]:
    origin = _origin_guild(row.emoji_id, row.guild_id) if row.kind == "emoji" else row.guild_id
    same_guild = 1 if (guild_id and origin == guild_id) else 0
    if not terms:
        return (0, 1, same_guild, row.uses, row.seen_count)
    summary_l = row.summary.strip().lower()
    exact = 1 if summary_l and any(t == summary_l for t in terms) else 0
    haystack = " ".join([summary_l, row.description, row.usage, row.ocr_text, " ".join(row.emotions)]).lower()
    hits = sum(1 for t in terms if t in haystack)
    return (exact, hits, same_guild, row.uses, row.seen_count)


def _format_candidate(row: _StickerRow, guild_id: str | None) -> str:
    if _is_same_guild_emoji(row.kind, row.emoji_id, row.guild_id, guild_id):
        tag = "本服内联表情"
    elif (row.frames or 0) > 1:
        tag = "动图表情包"
    else:
        tag = "图片表情包"
    parts: list[str] = []
    if row.summary and row.description:
        parts.append(f"{row.summary[:30]}：{row.description[:100]}")
    elif row.description:
        parts.append(row.description[:100])
    elif row.summary:
        parts.append(f"名称：{row.summary[:30]}")
    if row.usage:
        parts.append(f"适合：{row.usage[:100]}")
    if row.emotions:
        parts.append(f"情绪：{'、'.join(row.emotions[:5])}")
    if row.ocr_text:
        parts.append(f"文字：{row.ocr_text[:40]}")
    detail = "｜".join(parts) or "（无描述）"
    return f"#{row.id} [{tag}] {detail}"


@chat_tool("search_stickers")
async def search_stickers(ctx: RunContext[Deps], query: str = "", limit: int = 8) -> str:
    """按表情名、情绪、场景或关键词查询已收集的表情包与自定义表情（可用于查 `[表情:xxx]` 的含义，或挑一张配进回复）。
    query 可填表情名、情绪或场景词（如“doge”“笑哭”“点赞”“吐槽”），留空返回常用表情；limit 最多 15。"""
    cap = min(max(limit, 1), 15)
    guild_id = ctx.deps.guild_id
    pool_limit = ctx.deps.settings.tools.stickers_pool_limit
    pool = await (_load_pool(guild_id) if pool_limit == _POOL_LIMIT else _load_pool(guild_id, limit=pool_limit))
    if not pool:
        return "表情包库里还没有可用的表情包"
    terms = [t.lower() for t in _QUERY_SPLIT.split(query.strip()) if t]
    scored = [(_score(row, terms, guild_id), row) for row in pool]
    if terms:
        matched = [item for item in scored if item[0][1] > 0]
        if not matched:
            return f"没有找到匹配「{query.strip()}」的表情包"
        scored = matched
    scored.sort(key=lambda item: item[0], reverse=True)
    lines = [_format_candidate(row, guild_id) for _, row in scored[:cap]]
    return "匹配到的表情包（查意思直接看描述与用法；要发图再用 pick_sticker(编号)，最多 1 张）：\n" + "\n".join(lines)


async def _load_sticker(sticker_id: int) -> tuple[Sticker | None, bytes | None]:
    async with get_session() as session:
        sticker = await session.get(Sticker, sticker_id)
        if sticker is None:
            return None, None
        blob = await session.scalar(select(StickerBlob.data).where(StickerBlob.sticker_id == sticker_id))
    return sticker, blob


@chat_tool("pick_sticker")
async def pick_sticker(ctx: RunContext[Deps], sticker_id: int) -> str:
    """选用 search_stickers 返回的某张表情包。本服自定义表情返回内联 (emj) 标记，图片/动图返回 img#N 句柄。"""
    sticker, blob = await _load_sticker(sticker_id)
    if sticker is None or not sticker.enabled:
        raise ToolError(f"找不到可用的表情包 #{sticker_id}")

    if _is_same_guild_emoji(sticker.kind, sticker.emoji_id, sticker.guild_id, ctx.deps.guild_id):
        name = (sticker.summary or "emoji").replace("(", "").replace(")", "").strip() or "emoji"
        token = f"(emj){name}(emj)[{sticker.emoji_id}]"
        return f"已选本服自定义表情，直接把这段放进回复正文即可：{token}"

    if not blob:
        raise ToolError(f"表情包 #{sticker_id} 缺少图片数据，请换一张或纯文字回复")

    sha = blob_id(blob)
    ctx.deps.made_images[sha] = blob
    key = ctx.deps.handles.add("image", {"blob": sha, "caption": "", "made": "sticker"})
    assert key is not None
    ctx.deps.notes.shown(ctx.tool_call_id, [(blob, "")], mime=media_type(blob))
    return (
        f'已选表情包图片：{key}。放进回复某一节的 images（如 ["{key}"]）即可随回复发出；'
        f'若只想单独发这张图不配文字，把该节 content 留空并设 style="text"。'
    )


@chat_tool("save_sticker")
async def save_sticker(
    ctx: RunContext[Deps],
    handle: str,
    description: str = "",
    usage: str = "",
    emotions: list[str] | None = None,
    ocr_text: str = "",
) -> str:
    """把对话里的某张图片或动图录入表情包库。handle 是 img#N、file#N 或聊天记录消息编号 #N（多张图用 #N/2）。
    建议顺手填好 description（一句话描述画面）、usage（群聊适用场景）和 emotions（1-4 个情绪标签），填全后立即生效。"""
    if not ctx.deps.settings.features.sticker_collect:
        raise ToolError("当前频道未开启表情包收集功能，无法录入")
    h = await resolve_ref(ctx, handle, "image", "file")
    blob, name = await media_bytes(h, MAX_BYTES, made=ctx.deps.made_images)
    if needs_image_check(ctx.deps.settings):
        verdict = await check_image(blob)
        if verdict.model:
            ctx.deps.vision_tokens.append((verdict.model, verdict.input_tokens, verdict.output_tokens))
        if not verdict.safe:
            reason = "审核未通过" if verdict.category != "error" else "审核出错"
            raise ToolError(f"这张图{reason}（{verdict.category}），不能录入表情包库")
    saved = await save_image_sticker(
        blob,
        guild_id=ctx.deps.guild_id,
        name=name,
        description=description,
        usage=usage,
        emotions=emotions,
        ocr_text=ocr_text,
    )
    ctx.deps.actions.append(f"save_sticker:#{saved.sticker_id}")
    kind_label = "动图表情包" if saved.frames > 1 else "图片表情包"
    if saved.existed:
        detail = f"（{saved.description[:40]}）" if saved.description else ""
        return f"这张图已经在表情包库里了：#{saved.sticker_id} [{kind_label}]{detail}，无需重复录入。"
    action = "已重新收录" if saved.restored else "已录入"
    if saved.status == "ready":
        return f"{action}表情包库：#{saved.sticker_id} [{kind_label}]（{saved.description[:40]}），现已可被检索和选用。"
    return f"{action}表情包库：#{saved.sticker_id} [{kind_label}]，等待后台视觉打标完成后即可检索选用。"


def _tool(fn: Callable[..., Awaitable[ToolResult]]) -> Tool[Deps]:
    return Tool(fn, takes_ctx=True)


def capability(*, extra_instructions: str = "") -> Capability[Deps]:
    fns = (search_stickers, pick_sticker, save_sticker)
    text = INSTRUCTIONS if not extra_instructions.strip() else f"{INSTRUCTIONS}\n\n{extra_instructions.strip()}"
    return Capability[Deps](
        id=ID,
        description=DESCRIPTION,
        instructions=text,
        tools=[_tool(fn) for fn in fns],
        defer_loading=True,
    )
