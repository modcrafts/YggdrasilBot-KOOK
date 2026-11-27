"""Tests for KOOK forum/thread channel isolation, privacy, and read-only skill tools."""

from collections.abc import Callable

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from tests.support.kook import FakeKook, make_sender
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.skills import kook as kook_skill
from yggdrasil.forum import accessible_forum_channels
from yggdrasil.kook import segments as seg
from yggdrasil.kook.model import ForumPost, ForumThread, Guild, ThreadCategory, ThreadMedia, ThreadTag
from yggdrasil.memory.common import memory_channels
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot
from yggdrasil.settings import Settings
from yggdrasil.transcript.notes import TraceNotes

GUILD, CAT, CH_TEXT, CH_FORUM_PUB, CH_FORUM_PRIV = "g-1", "cat-1", "ch-text", "ch-forum-pub", "ch-forum-priv"


def _setup_snapshot(snapshot: Callable[..., Snapshot]) -> Snapshot:
    return snapshot(
        guilds={GUILD: GuildInfo(GUILD, "测试服", "", {}, enabled=True)},
        channels={
            CAT: ChannelInfo(CAT, GUILD, None, True, 0, "常规", True, {}, channel_type=0),
            CH_TEXT: ChannelInfo(CH_TEXT, GUILD, CAT, False, 1, "闲聊", None, {}, channel_type=1),
            CH_FORUM_PUB: ChannelInfo(CH_FORUM_PUB, GUILD, CAT, False, 2, "攻略区", None, {}, channel_type=4),
            CH_FORUM_PRIV: ChannelInfo(
                CH_FORUM_PRIV,
                GUILD,
                CAT,
                False,
                3,
                "内部工单",
                None,
                {"memory": {"share_in_guild": False, "readable_by_roles": ["role-admin"]}},
                channel_type=4,
            ),
        },
    )


def _ctx(kook: FakeKook, *, role_ids: tuple[str, ...] = ()) -> RunContext[Deps]:
    deps = Deps(
        kook=kook,
        settings=Settings(),
        channel_id=CH_TEXT,
        guild_id=GUILD,
        user_id="u-1",
        task="chat",
        role_ids=role_ids,
        handles=Handles(),
        notes=TraceNotes(),
    )
    return RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id="tc-1")


def test_forum_channels_isolated_from_text_pipelines_and_respect_privacy(snapshot: Callable[..., Snapshot]) -> None:
    snap = _setup_snapshot(snapshot)
    # Text channel listings & memory pipelines exclude forum channels
    assert [c.channel_id for c in snap.guild_channels(GUILD)] == [CH_TEXT]
    assert memory_channels(snap) == [CH_TEXT]
    # Dedicated forum listing returns both enabled forum channels
    assert [c.channel_id for c in snap.guild_forum_channels(GUILD)] == [CH_FORUM_PUB, CH_FORUM_PRIV]

    # Regular member sees only public forum; holder of readable_by_roles sees private forum too
    assert [c.channel_id for c in accessible_forum_channels(GUILD, CH_TEXT)] == [CH_FORUM_PUB]
    assert [c.channel_id for c in accessible_forum_channels(GUILD, CH_TEXT, role_ids=("role-admin",))] == [
        CH_FORUM_PUB,
        CH_FORUM_PRIV,
    ]


async def test_list_and_read_forum_thread_tools(
    snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_snapshot(snapshot)
    cached_batches: list[list[str]] = []

    async def fake_cache(threads: list[ForumThread]) -> int:
        cached_batches.append([t.thread_id for t in threads])
        return len(threads)

    monkeypatch.setattr(kook_skill, "cache_threads", fake_cache)
    alice = make_sender("u-1", username="alice")
    bob = make_sender("u-2", username="bob")
    cat = ThreadCategory(id="fc-1", name="入门")
    thread = ForumThread(
        thread_id="th-101",
        channel_id=CH_FORUM_PUB,
        guild_id=GUILD,
        post_id="p-main",
        title="新人部署指南",
        author=alice,
        preview_content="三步完成部署",
        content='[{"type":"card"}]',
        content_text="第一步安装 Docker，第二步配置环境变量。",
        content_segments=(seg.image("https://img.kookapp.cn/inline.png", name="step1"),),
        category=cat,
        tags=(ThreadTag(id=1, name="精华"),),
        medias=(ThreadMedia(type=1, src="https://img.kookapp.cn/cover.png", title="架构图"),),
        create_time=1_700_000_000,
        latest_active_time=1_700_001_000,
        post_count=2,
        collect_num=7,
    )
    post = ForumPost(
        post_id="p-201",
        thread_id="th-101",
        author=bob,
        content_text="非常清楚，已成功跑起来！",
        create_time=1_700_000_500,
        replies=(
            ForumPost(
                post_id="p-202",
                thread_id="th-101",
                author=alice,
                content_text="有问题随时反馈",
                belong_to_post_id="p-201",
                reply_id="p-201",
                create_time=1_700_000_800,
            ),
        ),
    )
    kook = FakeKook(
        guild_list=[Guild(guild_id=GUILD, name="测试服")],
        thread_category_map={CH_FORUM_PUB: [cat]},
        forum_thread_map={CH_FORUM_PUB: [thread]},
        forum_post_map={("th-101", None): [post]},
    )

    ctx = _ctx(kook)
    # 1. List across all accessible forum channels (no channel arg)
    overview = await kook_skill.list_forum_threads(ctx)
    assert isinstance(overview, str)
    assert "攻略区" in overview
    assert "内部工单" not in overview
    assert "th-101" in overview
    assert "新人部署指南" in overview

    # 2. List a specific forum channel by name & category name
    filtered = await kook_skill.list_forum_threads(ctx, channel="#攻略区", category="入门")
    assert isinstance(filtered, str)
    assert "入门(fc-1)" in filtered
    assert "精华" in filtered

    # 3. Read full thread content + media handles + nested replies
    detail = await kook_skill.read_forum_thread(ctx, channel="攻略区", thread_id="th-101")
    assert isinstance(detail, str)
    assert "第一步安装 Docker" in detail
    assert "非常清楚，已成功跑起来！" in detail
    assert "有问题随时反馈" in detail
    assert "img#1" in detail
    assert "img#2" in detail

    # 4. Private forum channel cannot be read without authorized role
    denied = await kook_skill.list_forum_threads(ctx, channel="内部工单")
    assert isinstance(denied, str)
    assert "找不到可读取的帖子频道" in denied
    assert cached_batches == [["th-101"], ["th-101"], ["th-101"]]
    assert ctx.deps.forum_thread_channels == {"th-101": CH_FORUM_PUB}
