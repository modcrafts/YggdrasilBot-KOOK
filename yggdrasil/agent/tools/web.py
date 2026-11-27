"""Web and X tools: ``read_url``, ``read_x``, ``search_x``, ``web_search``."""

from datetime import datetime
from typing import Literal

from pydantic_ai import RunContext

from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool, people_for, resolve_ref
from yggdrasil.config import APP_CONFIG
from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook.model import ForumPost, KookError
from yggdrasil.kook.parser import parse_kook_anchor_link
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.schema import Hit, HitsDisplay, PageDisplay


def _find_post_in_tree(posts: list[ForumPost], target_post_id: str) -> tuple[ForumPost | None, ForumPost | None]:
    """Find ``target_post_id`` among top-level posts or nested replies; returns ``(parent_post, matched_post)``."""
    for p in posts:
        if p.post_id == target_post_id:
            return None, p
        for sub in p.replies:
            if sub.post_id == target_post_id:
                return p, sub
    return None, None


async def _read_kook_forum_link(
    ctx: RunContext[Deps], url: str, guild_id: str, channel_id: str, thread_id: str, post_id: str | None
) -> str:
    """Resolve a KOOK forum thread or reply anchor link (respecting forum privacy rules)."""
    from yggdrasil.forum import accessible_forum_channels, cache_threads
    from yggdrasil.people import clean_name
    from yggdrasil.runtime import snapshot

    forums = {
        c.channel_id: c for c in accessible_forum_channels(guild_id, ctx.deps.channel_id, role_ids=ctx.deps.role_ids)
    }
    if channel_id not in forums:
        raise ToolError("该 KOOK 帖子链接所属频道未启用或无权访问")

    try:
        thread = await ctx.deps.kook.forum_thread(channel_id, thread_id, guild_id=guild_id)
    except KookError as e:
        raise ToolError(f"无法读取该 KOOK 帖子（可能已删除或无权限）：{e}") from e
    ctx.deps.forum_thread_channels[thread.thread_id] = channel_id
    try:
        await cache_threads([thread])
    except Exception:
        pass

    ch_name = snapshot().channel_name(channel_id) or channel_id
    who = clean_name(thread.author.name) or thread.author.user_id or "未知作者"
    created = (
        datetime.fromtimestamp(thread.create_time).astimezone().strftime("%Y-%m-%d %H:%M") if thread.create_time else ""
    )
    body = (
        "（主楼内容已被删除）"
        if thread.content_deleted
        else (thread.content_text or thread.preview_content or "（无正文）")
    )
    if len(body) > 4000:
        body = body[:4000] + "\n…（正文过长已截断）"

    media_keys: list[str] = []
    if thread.content_segments:
        reg = ctx.deps.handles.register(list(thread.content_segments), urls=True)
        if reg:
            media_keys.append(reg.strip())
    for m in thread.medias:
        if m.src and m.type == 1:
            if key := ctx.deps.handles.add("image", {"url": m.src, "name": m.title, "source": "kook_thread"}):
                media_keys.append(key)

    lines = [
        f"来源帖子频道：#{ch_name}（channel_id={channel_id}）",
        f"帖子：{thread.title}（thread_id={thread.thread_id}）",
        f"作者：{who}({thread.author.user_id})"
        + (f" | 发帖：{created}" if created else "")
        + f" | 回复：{thread.post_count}",
        "",
        "### 主楼正文",
        body,
    ]
    if media_keys:
        lines.append("附带媒体：" + " ".join(media_keys))

    try:
        posts = await ctx.deps.kook.forum_posts(channel_id, thread_id, order="asc", page=1, limit=50)
    except KookError as e:
        lines.append(f"\n### 评论\n拉取评论失败：{e}")
    else:
        if post_id:
            parent, target = _find_post_in_tree(posts, post_id)
            if target is not None:
                pwho = clean_name(target.author.name) or target.author.user_id or "未知用户"
                pwhen = (
                    datetime.fromtimestamp(target.create_time).astimezone().strftime("%Y-%m-%d %H:%M")
                    if target.create_time
                    else ""
                )
                phandles = (
                    ctx.deps.handles.register(list(target.content_segments), urls=True)
                    if target.content_segments
                    else ""
                )
                lines.append(f"\n### 链接定位到的目标回复（post_id={post_id}）")
                if parent is not None:
                    par_who = clean_name(parent.author.name) or parent.author.user_id or "未知用户"
                    lines.append(f"所在楼层 [post_id={parent.post_id}] {par_who}：{(parent.content_text or '')[:300]}")
                lines.append(
                    f"[{pwhen}] {pwho}({target.author.user_id})：{target.content_text or '（无文字）'}{phandles}"
                )
            else:
                lines.append(
                    f"\n### 链接定位到的目标回复（post_id={post_id}）\n"
                    "未在前 50 条一级回复中定位到该楼层，可用 read_forum_thread 进一步翻看。"
                )
        elif posts:
            lines.append(f"\n### 前 {min(len(posts), 10)} 条评论")
            for p in posts[:10]:
                pwho = clean_name(p.author.name) or p.author.user_id or "未知用户"
                lines.append(f"- [post_id={p.post_id}] {pwho}：{(p.content_text or '（无文字）')[:300]}")

    ctx.deps.notes.show(
        ctx.tool_call_id,
        PageDisplay(
            url=url, final_url=url, title=f"KOOK 帖子 #{ch_name} · {thread.title}", via="kook", page=1, pages=1
        ),
    )
    return "\n".join(lines)


async def _read_kook_link(
    ctx: RunContext[Deps], url: str, guild_id: str, channel_id: str, target_id: str, post_id: str | None = None
) -> str:
    """Resolve a KOOK message, forum thread, or forum reply anchor link."""
    from yggdrasil.memory.search import accessible_channels, find_by_msg_id
    from yggdrasil.runtime import snapshot

    if not ctx.deps.guild_id or ctx.deps.guild_id != guild_id:
        raise ToolError("无法读取其他服务器的 KOOK 链接")

    ch_info = snapshot().channels.get(channel_id)
    if (ch_info is not None and ch_info.is_forum) or post_id is not None or target_id.isdigit():
        return await _read_kook_forum_link(ctx, url, guild_id, channel_id, target_id, post_id)

    allowed = set(accessible_channels(ctx.deps.guild_id, ctx.deps.channel_id, role_ids=ctx.deps.role_ids))
    if channel_id not in allowed:
        raise ToolError("该 KOOK 消息链接所属频道无权访问")

    kook_msg_id = target_id

    row = await find_by_msg_id(channel_id, kook_msg_id)
    if row is not None:
        ctx.deps.notes.messages(ctx.tool_call_id, [row.id])
        segments = list(row.segments or [])
        user_id = row.user_id
        sender_name = row.sender_name
        sent_at = row.sent_at.astimezone().strftime("%Y-%m-%d %H:%M")
        db_ref = f"（编号 #{row.id}，可用 get_messages_around({row.id}) 查看前后上下文）"
    else:
        try:
            msg = await ctx.deps.kook.message(channel_id=channel_id, msg_id=kook_msg_id, guild_id=guild_id)
        except KookError as e:
            raise ToolError(f"无法读取该 KOOK 消息（可能已删除或无权限）：{e}") from e
        segments = list(msg.segments)
        user_id = msg.sender.user_id
        sender_name = msg.sender.name
        sent_at = datetime.fromtimestamp(msg.time).astimezone().strftime("%Y-%m-%d %H:%M")
        db_ref = ""

    mentions = normalize(segments).mentions
    people = await people_for(ctx, [user_id, *mentions])
    body = normalize(segments, people.mention).text.strip() or "（无正文）"
    handles = ctx.deps.handles.register(segments, urls=True)
    ch_name = snapshot().channel_name(channel_id) or channel_id
    who = people.who(user_id, sender_name)
    ctx.deps.notes.show(
        ctx.tool_call_id,
        PageDisplay(url=url, final_url=url, title=f"KOOK 消息 #{ch_name}", via="kook", page=1, pages=1),
    )
    return f"来源频道：#{ch_name}{db_ref}\n发送者：{who}\n时间：{sent_at}\n消息 ID：{kook_msg_id}\n\n{body}{handles}"


@chat_tool("read_url")
async def read_url(ctx: RunContext[Deps], url_or_handle: str, page: int = 1, browser: bool = False) -> str:
    """读取网页正文（支持分页）或本服务器可见的 KOOK 消息/帖子/帖子回复锚点链接（kookapp.cn/direct/anchor/...）。
    参数可以是 URL、url#N / card#N 句柄，或聊天记录里的消息编号 #N（那条消息里的链接）。
    普通读取遇到验证页会自动换浏览器；读到的仍是验证页、空壳或缺了要靠 JavaScript 加载的内容时，用 browser=true
    直接让真浏览器打开（会跑 JavaScript，慢几秒）。网页里的图片注册成 img#N（附说明），可用 view_image 看，
    也能作 generate_image 的参考图。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools.remote_fetch import remote_fetcher
    from yggdrasil.tools.web import read_url as _read
    from yggdrasil.tools.web.model import ImageLink

    url = url_or_handle
    if "#" in url_or_handle and not url_or_handle.startswith("http"):
        h = await resolve_ref(ctx, url_or_handle, "url", "card")
        url = h.data.get("url") or ""
        if not url:
            raise ToolError("这个卡片没有链接")
    if parsed := parse_kook_anchor_link(url):
        return await _read_kook_link(ctx, url, *parsed)
    cfg = ctx.deps.settings.tools
    try:
        res = await _read(
            url,
            max_chars=cfg.read_url_max_chars,
            page=page,
            browser_url=APP_CONFIG.browser_url if cfg.browser_fallback else None,
            remote=remote_fetcher(snapshot().search_providers) if cfg.remote_fetch else None,
            browser=browser,
        )
    except ImageLink as e:
        key = ctx.deps.handles.add("image", {"url": e.url, "source": "web"})
        return f"这是图片链接，不是网页：已注册为 {key}，用 view_image({key}) 查看。"
    page_info = PageDisplay(
        url=url, final_url=res.final_url, title=res.title, via=res.via, page=res.page, pages=res.pages
    )
    ctx.deps.notes.show(ctx.tool_call_id, page_info)
    more = _pages_note(res.page, res.pages, capped=res.truncated)
    images = _image_handles(ctx, res.images, "web", res.image_labels)
    return f"标题：{res.title}\n地址：{res.final_url}{more}\n\n{res.text}{images}"


def _image_handles(ctx: RunContext[Deps], urls: list[str], source: str, labels: list[str] | None = None) -> str:
    """Register third-party image URLs as img#N handles; the line to append. 图 N is the numbering in the text
    (X posts); a generic page's pictures carry a label instead (alt text, size)."""
    keys = [ctx.deps.handles.add("image", {"url": url, "source": source}) for url in urls]
    labels = labels or []
    shown = "、".join(
        f"图 {i} = {key}" + (f"（{labels[i - 1]}）" if i <= len(labels) and labels[i - 1] else "")
        for i, key in enumerate(keys, 1)
        if key
    )
    return f"\n\n图片句柄（用 view_image 查看，也能作 generate_image 的参考图）：{shown}" if shown else ""


def _pages_note(page: int, pages: int, *, capped: bool) -> str:
    """Where the model is in the text. A one-page text says so: otherwise models ask for a page 2 that is not there."""
    if pages > 1:
        return f"（第 {page}/{pages} 页，可用 page 参数翻页）"
    return "" if capped else "（只有这一页，没有第 2 页）"


@chat_tool("read_x")
async def read_x(ctx: RunContext[Deps], url_or_id: str, page: int = 1) -> str:
    """读 X（推特）上的一条帖子：作者、时间、互动数、全文（含长推和文章）、引用的帖子、投票、社区笔记，以及同一对话里的
    其他帖子；图片注册成 img#N，用 view_image 看。参数是帖子链接（x.com、twitter.com 等）、帖子 id、
    url#N / card#N 句柄，或聊天记录里的消息编号 #N（那条消息里的链接）。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools import x
    from yggdrasil.tools.web import paginate

    ref = url_or_id.strip()
    if "#" in ref and not ref.startswith("http"):
        ref = str((await resolve_ref(ctx, ref, "url", "card")).data.get("url") or "")
    pid = x.post_id(ref)
    if pid is None:
        raise ToolError("这不是 X 帖子的链接或 id")
    post = await x.read_post(pid, x.provider(snapshot().search_providers))
    pages = paginate(x.format_post(post), ctx.deps.settings.tools.read_url_max_chars)
    if not 1 <= page <= len(pages):
        raise ToolError(f"页码超出范围，共 {len(pages)} 页")
    title = f"{x.who(post)} 在 X 上的帖子"
    ctx.deps.notes.show(
        ctx.tool_call_id,
        PageDisplay(url=ref, final_url=post.url, title=title, via=post.via, page=page, pages=len(pages)),
    )
    more = _pages_note(page, len(pages), capped=False)
    images = _image_handles(ctx, [m.url for m in post.images()], "x") if page == 1 else ""
    return f"标题：{title}\n地址：{post.url}{more}\n\n{pages[page - 1]}{images}"


@chat_tool("search_x")
async def search_x(
    ctx: RunContext[Deps], query: str, kind: Literal["news", "posts"] = "news", hours: int | None = None
) -> str:
    """搜 X（推特）上的最新动态和讨论：Grok 在 X 上搜索后用中文总结，附上引用的帖子链接。很慢（常要 20–40 秒），
    只在确实需要 X 上的内容时用，普通资料用 web_search。query 直接写想问的问题
    （“某某最近说了什么”“XX 发布了吗，大家怎么评价”）。hours 是往前看几小时（最多 720，不填用工具默认配置）。
    Grok 不可用时改用 X API：kind="news"（默认）是 X 整理的新闻摘要，
    kind="posts" 是最近 7 天按相关度排的原帖（最多 10 条）。要看帖子全文、图片或评论上下文，再用 read_x。"""
    from yggdrasil.core.log import LOGGER
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools import grok, x
    from yggdrasil.tools.search import reserve_quota

    query = query.strip()
    if not query:
        raise ToolError("搜索关键词不能为空")
    lookback = ctx.deps.settings.tools.search_x_default_hours if hours is None else hours
    providers = snapshot().search_providers
    api = x.provider(providers)
    if (g := grok.provider(providers)) is not None:
        try:
            if not await reserve_quota(g):
                raise ToolError("Grok 本月额度已用完")
            answer = await grok.x_search(g, query, hours=lookback)
        except ToolError as e:
            if api is None:
                raise
            LOGGER.warning("grok x_search failed, falling back to the X API: {}", e)
        else:
            hits = [Hit(title="Grok 总结", snippet=answer.text)] + [Hit(title=u, url=u) for u in answer.citations]
            ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
            return grok.format_answer(answer)
    if api is None:
        raise ToolError("没有配置 Grok 或 X API（控制台 → 搜索源 → 添加 Grok 或 X API 类型）")
    if kind == "news":
        n = ctx.deps.settings.tools.search_results
        stories = await x.search_news(api, query, hours=lookback, n=n, reserve=reserve_quota)
        if not stories:
            return "X 上没有相关的新闻"
        hits = [
            Hit(title=s.name, url=f"https://x.com/i/status/{s.post_ids[0]}" if s.post_ids else "", snippet=s.summary)
            for s in stories
        ]
        ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
        return x.format_news(stories)
    posts = await x.search_posts(api, query, hours=lookback, reserve=reserve_quota)
    if not posts:
        return "X 上没有相关的帖子"
    hits = [Hit(title=x.who(p), url=p.url, snippet=p.text) for p in posts]
    ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=hits))
    return x.format_posts(posts)


@chat_tool("web_search")
async def web_search(ctx: RunContext[Deps], query: str) -> str:
    """用搜索引擎查资料，返回标题、链接和摘要。需要细节时再用 read_url 读原文。"""
    from yggdrasil.runtime import snapshot
    from yggdrasil.tools.search import web_search as _search

    hits = await _search(query, snapshot().search_providers, n=ctx.deps.settings.tools.search_results)
    if not hits:
        return "没有搜索结果"
    shown = [Hit(title=h.title, url=h.url, snippet=h.snippet) for h in hits]
    ctx.deps.notes.show(ctx.tool_call_id, HitsDisplay(query=query, hits=shown))
    return "\n\n".join(f"{i + 1}. {h.title}\n{h.url}\n{h.snippet}" for i, h in enumerate(hits))
