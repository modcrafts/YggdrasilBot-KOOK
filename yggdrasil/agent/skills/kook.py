"""The ``kook`` skill: real-time KOOK server/channel info, pinned messages, members, avatars, reactions, and uploads."""

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import PurePosixPath

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability
from sqlalchemy import select

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import ToolResult, chat_tool, people_for
from yggdrasil.db.models import Message
from yggdrasil.db.session import get_session
from yggdrasil.forum import accessible_forum_channels, cache_threads
from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook.model import ForumPost, KookError
from yggdrasil.kook.parser import parse_kook_message_link
from yggdrasil.people import clean_name
from yggdrasil.runtime import ChannelInfo, snapshot
from yggdrasil.sandbox import SandboxError
from yggdrasil.sandbox.text import fmt_size
from yggdrasil.tools.errors import ToolError

ID = "kook"
DESCRIPTION = (
    "KOOK 服务器与频道的信息、帖子频道阅读、操作与 KMarkdown 语法参考："
    "成员资料（服务器昵称、用户名、角色头衔、在线状态、入服/活跃时间）、成员或服务器头像、频道名称与主题、"
    "同服务器文字频道与帖子频道列表、帖子频道浏览与阅读（主楼正文、媒体与评论楼中楼）、频道置顶消息、"
    "给消息贴/取消表情回应（reaction）、KOOK KMarkdown 特殊标签写法、把工作区文件上传发送到当前频道。"
    "问到或要做这些事时先加载它（不要联网查 KOOK 文档）。"
)
INSTRUCTIONS = """\
KOOK 工具直接实时查询 KOOK 服务器；user_id 是成员 ID（「名字(user_id)」括号里的数字）。
KOOK KMarkdown 常用语法（直接使用，不要联网查 KOOK 文档）：
- @ 成员：`(met)user_id(met)`；@ 在线/全体：`(met)here(met)` / `(met)all(met)`（无明确要求勿用）；
  @ 角色：`(rol)role_id(rol)`。
- 频道跳转：直接写 `#频道名`（系统自动转为 `(chn)channel_id(chn)`）。
- 样式扩展：`**加粗**`、`*斜体*`、`~~删除线~~`、`(ins)下划线(ins)`、`(spl)剧透(spl)`、
  `[文字](https://...)`、`---`、`> 引用`、`` `代码` ``。
- 服务器表情：`(emj)表情名(emj)[表情ID]`；
  卡片彩色文字（仅 card 有效）：`(font)文字(font)[primary|success|danger|warning|info|secondary|tips|pink|purple]`。
工具说明：
- list_forum_threads 查看本服务器可见的帖子频道（type=4）及其分区、帖子列表（标题/预览/thread_id）；
  不传 channel 时列出全部可见帖子频道及其最新帖子。
- read_forum_thread 按 channel + thread_id 读取帖子完整主楼正文（自动解析卡片正文与图片句柄 img#N）以及评论与楼中楼；
  若需展开某一楼层的完整回复，再传 post_id。
- get_avatar 给出图片句柄 img#N：要看图用 view_image，要发给频道就放进回答某一节的 images。
- react_message 给触发消息或本频道指定消息（#编号、KOOK 消息链接或 KOOK 消息 ID）贴/取消表情回应；
  emoji 可填 Unicode 表情或服务器自定义表情 ID。只贴表情且无需回复时选 mode=done。
- upload_file 只上传工作区里已有的文件到当前频道：传路径，不要传内容；要上传的东西先在工作区里生成。
- 只完成了操作（如上传文件或贴表情）、没有别的要说时选 mode=done。"""

UPLOAD_MAX_BYTES = 20 * 1024 * 1024
PINNED_SHOWN = 5
_PIN_CHARS = 300
_THREAD_PREVIEW_CHARS = 180
_THREAD_BODY_CHARS = 6000
_POST_CHARS = 800
_ROLES = {"owner": "服主", "admin": "管理员", "member": "成员"}
_DB_MSG_REF = re.compile(r"#?(\d+)")


def _scope(ctx: RunContext[Deps]) -> tuple[str, str]:
    if not ctx.deps.channel_id or not ctx.deps.guild_id:
        raise ToolError("只能在服务器频道里用")
    return ctx.deps.guild_id, ctx.deps.channel_id


def _when(ts: int) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


@chat_tool("member_info")
async def member_info(ctx: RunContext[Deps], user_id: str) -> str:
    """查服务器成员资料：服务器昵称、用户名、身份与角色、入服时间、活跃时间。user_id 是成员 ID。"""
    guild_id, _ = _scope(ctx)
    try:
        m = await ctx.deps.kook.member(guild_id, user_id.strip())
    except KookError as e:
        raise ToolError(f"查不到 {user_id} 的成员资料（可能不在本服务器）：{e}") from e
    nickname, username = clean_name(m.nickname), clean_name(m.username)
    uname_display = f"{username}#{m.identify_num}" if username and m.identify_num else (username or "（未知）")
    lines = [
        (await people_for(ctx, [m.user_id])).who(m.user_id, nickname or username),
        f"服务器昵称：{nickname or '（未设置）'}",
        f"用户名：{uname_display}",
        f"身份：{_ROLES.get(m.role, m.role)}",
    ]
    if m.role_names:
        lines.append(f"角色：{'、'.join(m.role_names)}")
    if m.is_bot:
        lines.append(f"账号类型：机器人{f'（{m.tag_text}）' if m.tag_text else ''}")
    elif m.tag_text:
        lines.append(f"官方标签：{m.tag_text}")
    if m.online:
        lines.append(f"在线状态：在线{f'（{m.os}）' if m.os else ''}")
    if m.joined_at:
        lines.append(f"入服时间：{_when(m.joined_at)}")
    if m.active_time:
        lines.append(f"最近活跃：{_when(m.active_time)}")
    return "\n".join(lines)


@chat_tool("channel_info")
async def channel_info(ctx: RunContext[Deps]) -> str:
    """查当前频道和所在服务器的信息：服务器名、频道名与主题、同服务器文字频道列表、本频道置顶消息。"""
    guild_id, channel_id = _scope(ctx)
    kook, handles = ctx.deps.kook, ctx.deps.handles
    out: list[str] = []
    try:
        g = await kook.guild(guild_id)
        out.append(f"服务器：{g.name}（ID {guild_id}）")
        if g.topic:
            out.append(f"服务器简介：{_clip(g.topic, 300)}")
    except KookError as e:
        out.append(f"服务器信息获取失败（{e}）")

    try:
        ch = await kook.channel(channel_id)
        out.append(f"当前频道：#{ch.name}（ID {channel_id}）")
        if ch.topic:
            out.append(f"频道主题：{_clip(ch.topic, 400)}")
    except KookError:
        name = snapshot().channel_name(channel_id) or channel_id
        out.append(f"当前频道：#{name}（ID {channel_id}）")

    siblings = snapshot().guild_channels(guild_id, enabled_only=True)
    if siblings:
        out.append("\n本服务器已启用的文字频道：" + "、".join(f"#{c.name}" for c in siblings))
    forums = accessible_forum_channels(guild_id, channel_id, role_ids=ctx.deps.role_ids)
    if forums:
        out.append(
            "\n本服务器可读取的帖子频道："
            + "、".join(f"#{c.name or c.channel_id}({c.channel_id})" for c in forums)
        )

    try:
        pinned = (await kook.pinned_messages(channel_id, limit=PINNED_SHOWN))[:PINNED_SHOWN]
        if pinned:
            ids = [m.sender.user_id for m in pinned] + [u for m in pinned for u in normalize(m.segments).mentions]
            people = await people_for(ctx, ids)
            out.append("\n本频道置顶消息：")
            for m in pinned:
                text = _clip(normalize(m.segments, people.mention).text, _PIN_CHARS)
                who = people.who(m.sender.user_id, m.sender.name)
                out.append(f"- [{_when(m.time)}] {who}: {text}{handles.register(m.segments)}")
        else:
            out.append("\n本频道置顶消息：无")
    except KookError as e:
        out.append(f"\n本频道置顶消息：获取失败（{e}）")
    return "\n".join(out)


@chat_tool("get_avatar")
async def get_avatar(ctx: RunContext[Deps], user_id: str | None = None) -> str:
    """取头像，得到图片句柄 img#N。user_id 是成员 ID；不传则取本服务器的图标。"""
    guild_id, _ = _scope(ctx)
    if not user_id:
        try:
            g = await ctx.deps.kook.guild(guild_id)
        except KookError as e:
            raise ToolError(f"拿不到服务器图标：{e}") from e
        if not g.icon:
            raise ToolError("本服务器没有设置图标")
        key = ctx.deps.handles.add("image", {"url": g.icon, "source": "kook"})
        return f"本服务器的图标：{key}"
    uid = user_id.strip()
    try:
        m = await ctx.deps.kook.member(guild_id, uid)
    except KookError as e:
        raise ToolError(f"拿不到 {uid} 的头像：{e}") from e
    if not m.avatar:
        raise ToolError(f"{uid} 没有头像链接")
    key = ctx.deps.handles.add("image", {"url": m.avatar, "source": "kook"})
    return f"{(await people_for(ctx, [uid])).who(uid, m.display_name)} 的头像：{key}"


@chat_tool("upload_file")
async def upload_file(ctx: RunContext[Deps], path: str, name: str | None = None) -> str:
    """把当前工作区里的一个文件上传并发送到本频道（最大 20MB）。path 是工作区里的路径；name 是发送的文件名。"""
    _, channel_id = _scope(ctx)
    session = ctx.deps.sandbox
    if session is None:
        raise ToolError("这个频道没开工作区，没法上传文件")
    try:
        data = await session.get_bytes(path, max_bytes=UPLOAD_MAX_BYTES)
    except SandboxError as e:
        raise ToolError(str(e)) from e
    shown = PurePosixPath((name or "").replace("\\", "/")).name.strip() or PurePosixPath(path).name
    try:
        url = await ctx.deps.kook.upload_asset(data, shown)
        await ctx.deps.kook.send_file_url(
            channel_id=channel_id,
            user_id=ctx.deps.user_id,
            url=url,
            quote=ctx.deps.trigger_message_id,
        )
    except KookError as e:
        raise ToolError(f"上传发送失败：{e}") from e
    ctx.deps.actions.append(f"upload_file {shown}")
    return f"已上传并发送到本频道：{shown}（{fmt_size(len(data))}）"


async def _resolve_kook_msg_target(ctx: RunContext[Deps], message_id: str | None) -> tuple[str | None, str]:
    raw = (message_id or "").strip()
    if not raw:
        if not ctx.deps.trigger_message_id:
            raise ToolError("当前没有可操作的目标消息")
        return ctx.deps.channel_id, ctx.deps.trigger_message_id
    if parsed := parse_kook_message_link(raw):
        from yggdrasil.memory.search import accessible_channels

        link_guild, link_channel, link_msg = parsed
        if not ctx.deps.guild_id or ctx.deps.guild_id != link_guild:
            raise ToolError("无法操作其他服务器的 KOOK 消息链接")
        allowed = set(accessible_channels(ctx.deps.guild_id, ctx.deps.channel_id, role_ids=ctx.deps.role_ids))
        if link_channel not in allowed:
            raise ToolError("该 KOOK 消息链接所属频道无权操作")
        return link_channel, link_msg
    if m := _DB_MSG_REF.fullmatch(raw):
        if not ctx.deps.channel_id:
            raise ToolError("私聊里不能用消息编号 #N，请留空操作当前消息")
        async with get_session() as s:
            kook_id = await s.scalar(
                select(Message.kook_message_id).where(
                    Message.channel_id == ctx.deps.channel_id,
                    Message.id == int(m.group(1)),
                    Message.recalled.is_(False),
                )
            )
        if not kook_id:
            raise ToolError(f"本频道找不到消息 #{m.group(1)}")
        return ctx.deps.channel_id, kook_id
    return ctx.deps.channel_id, raw


@chat_tool("react_message")
async def react_message(ctx: RunContext[Deps], emoji: str, message_id: str | None = None, remove: bool = False) -> str:
    """给消息贴或取消 KOOK 表情回应（reaction）。emoji 填 Unicode 表情（如 👍、😂）或服务器自定义表情 ID；
    message_id 留空表示当前触发消息，也可填本频道历史消息编号（#N）、KOOK 消息链接或 KOOK 消息 ID；
    remove=True 取消 bot 自己贴的表情。"""
    clean = emoji.strip()
    if not clean or len(clean) > 64 or any(ch.isspace() for ch in clean):
        raise ToolError("请传入单个合法的 Unicode 表情或 KOOK 自定义表情 ID")
    target_channel, target_msg = await _resolve_kook_msg_target(ctx, message_id)
    try:
        if remove:
            await ctx.deps.kook.unreact(channel_id=target_channel, msg_id=target_msg, emoji=clean)
        else:
            await ctx.deps.kook.react(channel_id=target_channel, msg_id=target_msg, emoji=clean)
    except KookError as e:
        raise ToolError(f"{'取消' if remove else '添加'}表情回应失败：{e}") from e
    ctx.deps.actions.append(f"{'unreact' if remove else 'react'} {clean}")
    return f"已{'取消' if remove else '贴上'}表情回应：{clean}"


def _resolve_forum_channel(ctx: RunContext[Deps], channel: str) -> ChannelInfo:
    guild_id, channel_id = _scope(ctx)
    forums = accessible_forum_channels(guild_id, channel_id, role_ids=ctx.deps.role_ids)
    if not forums:
        raise ToolError("本服务器目前没有已启用且允许当前频道读取的帖子频道")
    wanted = channel.strip().lstrip("#")
    for ch in forums:
        if ch.channel_id == wanted or (ch.name and ch.name.lower() == wanted.lower()):
            return ch
    available = "、".join(f"#{c.name or c.channel_id}({c.channel_id})" for c in forums)
    raise ToolError(f"找不到可读取的帖子频道「{channel}」；可用帖子频道：{available}")


async def _try_cache_threads(threads: list) -> None:
    try:
        await cache_threads(threads)
    except Exception:
        pass


def _format_post_block(post: ForumPost, ctx: RunContext[Deps], *, indent: str = "") -> list[str]:
    who = clean_name(post.author.name) or post.author.user_id or "未知用户"
    when = _when(post.create_time) if post.create_time else ""
    body = _clip(post.content_text or "（无文字内容）", _POST_CHARS)
    handles = ctx.deps.handles.register(list(post.content_segments)) if post.content_segments else ""
    lines = [f"{indent}- [post_id={post.post_id}] [{when}] {who}({post.author.user_id}): {body}{handles}"]
    for sub in post.replies:
        lines.extend(_format_post_block(sub, ctx, indent=indent + "  "))
    return lines


@chat_tool("list_forum_threads")
async def list_forum_threads(
    ctx: RunContext[Deps],
    channel: str | None = None,
    category: str | None = None,
    sort: str = "latest",
    limit: int = 15,
) -> str:
    """列出本服务器可读取的 KOOK 帖子频道（type=4）及其帖子列表。
    channel 留空时列出所有可见帖子频道及各自最新帖子；传入频道名或频道 ID 时列出该帖子频道的分区与帖子列表。
    category 可按分区名或分区 ID 筛选；sort 支持 latest（最新回复，默认）或 newest（发帖时间）。"""
    guild_id, caller_ch = _scope(ctx)
    forums = accessible_forum_channels(guild_id, caller_ch, role_ids=ctx.deps.role_ids)
    if not forums:
        return "本服务器目前没有已启用且可见的帖子频道"

    sort_code = 2 if sort.strip().lower() in ("newest", "create", "created", "2") else 1
    cap = min(max(limit, 1), 30)

    if not channel or not channel.strip():
        per_ch = min(5, cap)
        sections: list[str] = []
        for ch in forums:
            header = f"## 帖子频道 #{ch.name or ch.channel_id}（channel_id={ch.channel_id}）"
            try:
                threads = await ctx.deps.kook.forum_threads(
                    ch.channel_id, guild_id=guild_id, sort=sort_code, limit=per_ch
                )
            except KookError as e:
                sections.append(f"{header}\n拉取失败：{e}")
                continue
            await _try_cache_threads(threads)
            if not threads:
                sections.append(f"{header}\n（暂无帖子）")
                continue
            lines = [header]
            for t in threads:
                cat = f"[{t.category.name}] " if t.category and t.category.name else ""
                who = clean_name(t.author.name) or t.author.user_id
                when = _when(t.latest_active_time or t.create_time) if (t.latest_active_time or t.create_time) else ""
                preview = _clip(t.preview_content or t.content_text, _THREAD_PREVIEW_CHARS)
                lines.append(
                    f"- [thread_id={t.thread_id}] {cat}**{t.title}**"
                    f" — {who} · {t.post_count} 回复 · 活跃 {when}"
                    + (f"\n  摘要：{preview}" if preview else "")
                )
            sections.append("\n".join(lines))
        return "\n\n".join(sections)

    target = _resolve_forum_channel(ctx, channel)
    cat_id: str | None = None
    cats = []
    try:
        cats = await ctx.deps.kook.thread_categories(target.channel_id)
    except KookError:
        cats = []
    if category and category.strip():
        wanted_cat = category.strip()
        matched = next((c for c in cats if c.id == wanted_cat or c.name.lower() == wanted_cat.lower()), None)
        cat_id = matched.id if matched else wanted_cat

    try:
        threads = await ctx.deps.kook.forum_threads(
            target.channel_id, guild_id=guild_id, category_id=cat_id, sort=sort_code, limit=cap
        )
    except KookError as e:
        raise ToolError(f"拉取帖子列表失败：{e}") from e
    await _try_cache_threads(threads)

    out = [f"帖子频道：#{target.name or target.channel_id}（channel_id={target.channel_id}）"]
    if cats:
        out.append("分区：" + "、".join(f"{c.name}({c.id})" for c in cats))
    if not threads:
        out.append("（该条件下没有帖子）")
        return "\n".join(out)
    for t in threads:
        cat = f"[{t.category.name}] " if t.category and t.category.name else ""
        tags = f" 标签：{'/'.join(tag.name for tag in t.tags if tag.name)}" if t.tags else ""
        who = clean_name(t.author.name) or t.author.user_id
        when = _when(t.latest_active_time or t.create_time) if (t.latest_active_time or t.create_time) else ""
        preview = _clip(t.preview_content or t.content_text, _THREAD_PREVIEW_CHARS)
        out.append(
            f"- [thread_id={t.thread_id}] {cat}**{t.title}**（{who}，{t.post_count} 回复，活跃 {when}）{tags}"
            + (f"\n  摘要：{preview}" if preview else "")
        )
    return "\n".join(out)


@chat_tool("read_forum_thread")
async def read_forum_thread(
    ctx: RunContext[Deps],
    channel: str,
    thread_id: str,
    include_posts: bool = True,
    post_id: str | None = None,
    posts_limit: int = 20,
) -> str:
    """读取指定 KOOK 帖子频道中某条帖子的完整主楼正文、媒体及评论。
    channel 填帖子频道名或频道 ID，thread_id 填帖子 ID（可先用 list_forum_threads 获取）。
    include_posts=True 时附带一级评论及楼中楼预览；传入 post_id 时只拉取该楼层的完整楼中楼回复。"""
    guild_id, _ = _scope(ctx)
    target = _resolve_forum_channel(ctx, channel)
    tid = thread_id.strip()
    if not tid:
        raise ToolError("请提供 thread_id")

    try:
        thread = await ctx.deps.kook.forum_thread(target.channel_id, tid, guild_id=guild_id)
    except KookError as e:
        raise ToolError(f"读取帖子 {tid} 失败：{e}") from e
    await _try_cache_threads([thread])

    who = clean_name(thread.author.name) or thread.author.user_id or "未知作者"
    created = _when(thread.create_time) if thread.create_time else ""
    active = _when(thread.latest_active_time) if thread.latest_active_time else ""
    header_parts = [f"帖子：{thread.title}（thread_id={thread.thread_id}，频道 #{target.name or target.channel_id}）"]
    meta = [f"作者：{who}({thread.author.user_id})"]
    if thread.category and thread.category.name:
        meta.append(f"分区：{thread.category.name}")
    if thread.tags:
        meta.append("标签：" + "、".join(t.name for t in thread.tags if t.name))
    if created:
        meta.append(f"发帖：{created}")
    if active:
        meta.append(f"最近活跃：{active}")
    meta.append(f"回复：{thread.post_count}，收藏：{thread.collect_num}")
    header_parts.append(" | ".join(meta))

    body_text = thread.content_text or thread.preview_content or "（无正文）"
    if thread.content_deleted:
        body_text = "（主楼内容已被删除）"
    elif len(body_text) > _THREAD_BODY_CHARS:
        body_text = body_text[:_THREAD_BODY_CHARS] + "\n…（正文过长已截断）"

    media_keys: list[str] = []
    if thread.content_segments:
        registered = ctx.deps.handles.register(list(thread.content_segments))
        if registered:
            media_keys.append(registered.strip())
    for m in thread.medias:
        if not m.src:
            continue
        if m.type == 1:
            key = ctx.deps.handles.add("image", {"url": m.src, "name": m.title, "source": "kook_thread"})
            if key:
                media_keys.append(key)
        else:
            media_keys.append(f"[{m.title or '媒体'}]({m.src})")

    out = [*header_parts, "", "### 主楼正文", body_text]
    if media_keys:
        out.append("附带媒体：" + " ".join(media_keys))

    if include_posts or post_id:
        pid = post_id.strip() if post_id and post_id.strip() else None
        try:
            posts = await ctx.deps.kook.forum_posts(
                target.channel_id, tid, post_id=pid, order=1, page=1, limit=min(max(posts_limit, 1), 50)
            )
        except KookError as e:
            out.append(f"\n### 评论\n拉取评论失败：{e}")
        else:
            title = f"楼层 {pid} 的回复" if pid else "评论与回复"
            if not posts:
                out.append(f"\n### {title}\n（暂无）")
            else:
                out.append(f"\n### {title}（共返回 {len(posts)} 条）")
                for p in posts:
                    out.extend(_format_post_block(p, ctx))
    return "\n".join(out)


def _tool(fn: Callable[..., Awaitable[ToolResult]]) -> Tool[Deps]:
    return Tool(fn, takes_ctx=True)


def capability() -> Capability[Deps]:
    fns = (member_info, channel_info, get_avatar, react_message, upload_file, list_forum_threads, read_forum_thread)
    return Capability[Deps](
        id=ID,
        description=DESCRIPTION,
        instructions=INSTRUCTIONS,
        tools=[_tool(fn) for fn in fns],
        defer_loading=True,
    )
