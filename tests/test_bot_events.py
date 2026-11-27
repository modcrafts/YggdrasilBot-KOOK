"""Tests for KOOK observed event filtering and automatic channel discovery/sync."""

from collections.abc import Callable
from typing import Any

import pytest

from tests.support.kook import FakeKook, make_incoming
from yggdrasil import events
from yggdrasil.bot import router
from yggdrasil.kook.model import Channel, Observed
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot, TriggerRule

BOT, GUILD_ON, GUILD_OFF = "bot-1", "g-on", "g-off"
CAT_ON, CH_ON, CH_OFF = "cat-on", "ch-on", "ch-off"


@pytest.fixture(autouse=True)
def _channels(snapshot: Callable[..., Snapshot]) -> None:
    router._discover_tried.clear()  # pyright: ignore[reportPrivateUsage]
    router._discover_inflight.clear()  # pyright: ignore[reportPrivateUsage]
    snapshot(
        guilds={
            GUILD_ON: GuildInfo(GUILD_ON, "启用服", "", {}, enabled=True),
            GUILD_OFF: GuildInfo(GUILD_OFF, "停用服", "", {}, enabled=False),
        },
        channels={
            CAT_ON: ChannelInfo(CAT_ON, GUILD_ON, None, True, 0, "常规分组", True, {}),
            CH_ON: ChannelInfo(CH_ON, GUILD_ON, CAT_ON, False, 1, "闲聊", None, {}),
            CH_OFF: ChannelInfo(CH_OFF, GUILD_OFF, None, False, 0, "静音", False, {}),
        },
        triggers=(
            TriggerRule(1, "chat", "global", None, "mention", (), None, True, "quoted_or_self", 10, 0, ("member",)),
        ),
    )


def _kept(kind: str, guild_id: str | None, channel_id: str | None = None, user_id: str | None = None) -> bool:
    return events.kept(Observed(kind=kind, guild_id=guild_id, channel_id=channel_id, user_id=user_id), BOT)


def test_bot_guild_membership_events_are_always_kept_channel_events_follow_enabled_channels() -> None:
    assert _kept("kook.self_joined_guild", GUILD_OFF)
    assert _kept("kook.self_exited_guild", GUILD_OFF)
    assert _kept("kook.deleted_guild", GUILD_OFF)
    assert _kept("kook.updated_guild", GUILD_OFF)
    assert _kept("kook.added_channel", GUILD_ON, CH_ON)
    assert _kept("kook.updated_channel", GUILD_ON, CH_ON)
    assert _kept("kook.pinned_message", GUILD_ON, CH_ON)
    assert not _kept("kook.pinned_message", GUILD_OFF, CH_OFF)


async def test_added_and_updated_channel_events_upsert_and_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    upserts: list[dict[str, Any]] = []
    reloads: list[str] = []
    published: list[str] = []

    async def fake_upsert(channel_id: str, **kw: Any) -> bool:
        upserts.append({"channel_id": channel_id, **kw})
        return True

    async def fake_load() -> Snapshot:
        reloads.append("load")
        return Snapshot()

    async def fake_publish(reason: str) -> None:
        published.append(reason)

    monkeypatch.setattr(events, "upsert_channel_record", fake_upsert)
    monkeypatch.setattr(events, "load", fake_load)
    monkeypatch.setattr(events, "publish_change", fake_publish)

    kook = FakeKook(bot_id=BOT)
    # New text channel under an existing enabled category
    added = Observed(
        kind="kook.added_channel",
        guild_id=GUILD_ON,
        channel_id="ch-new",
        data={"id": "ch-new", "guild_id": GUILD_ON, "name": "新频道", "parent_id": CAT_ON, "type": 1, "level": 5},
    )
    await events._apply_policy(kook, 1, added)  # pyright: ignore[reportPrivateUsage]
    assert len(upserts) == 1
    assert upserts[0] == {
        "channel_id": "ch-new",
        "guild_id": GUILD_ON,
        "name": "新频道",
        "topic": "",
        "position": 5,
        "is_category": False,
        "parent_id": CAT_ON,
        "channel_type": 1,
    }
    assert len(reloads) == 1
    assert published == ["channel ch-new kook.added_channel"]

    # Forum channels (type=4) are also synced with channel_type=4
    forum = Observed(
        kind="kook.added_channel",
        guild_id=GUILD_ON,
        channel_id="ch-forum",
        data={"id": "ch-forum", "guild_id": GUILD_ON, "name": "攻略帖", "parent_id": CAT_ON, "type": 4, "level": 6},
    )
    await events._apply_policy(kook, 2, forum)  # pyright: ignore[reportPrivateUsage]
    assert len(upserts) == 2
    assert upserts[1]["channel_type"] == 4

    # Voice channels (type=2) are ignored
    voice = Observed(
        kind="kook.added_channel",
        guild_id=GUILD_ON,
        channel_id="ch-voice",
        data={"id": "ch-voice", "guild_id": GUILD_ON, "name": "语音", "parent_id": CAT_ON, "type": 2},
    )
    await events._apply_policy(kook, 3, voice)  # pyright: ignore[reportPrivateUsage]
    assert len(upserts) == 2


async def test_batch_channel_events_and_role_emoji_cache_invalidation(monkeypatch: pytest.MonkeyPatch) -> None:
    upserts: list[dict[str, Any]] = []
    deleted: list[str] = []
    reloads: list[str] = []
    published: list[str] = []

    async def fake_upsert(channel_id: str, **kw: Any) -> bool:
        upserts.append({"channel_id": channel_id, **kw})
        return True

    async def fake_delete(channel_id: str) -> bool:
        deleted.append(channel_id)
        return True

    async def fake_load() -> Snapshot:
        reloads.append("load")
        return Snapshot()

    async def fake_publish(reason: str) -> None:
        published.append(reason)

    monkeypatch.setattr(events, "upsert_channel_record", fake_upsert)
    monkeypatch.setattr(events, "delete_channel_record", fake_delete)
    monkeypatch.setattr(events, "load", fake_load)
    monkeypatch.setattr(events, "publish_change", fake_publish)

    kook = FakeKook(bot_id=BOT)
    # Batch added: text + forum + voice (voice skipped)
    await events._apply_policy(  # pyright: ignore[reportPrivateUsage]
        kook,
        10,
        Observed(
            kind="kook.batch_added_channel",
            guild_id=GUILD_ON,
            data={
                "items": [
                    {"id": "c-a", "guild_id": GUILD_ON, "name": "文字A", "parent_id": CAT_ON, "type": 1},
                    {"id": "c-f", "guild_id": GUILD_ON, "name": "帖子F", "parent_id": CAT_ON, "type": 4},
                    {"id": "c-v", "guild_id": GUILD_ON, "name": "语音V", "parent_id": CAT_ON, "type": 2},
                ]
            },
        ),
    )
    assert [(u["channel_id"], u["channel_type"]) for u in upserts] == [("c-a", 1), ("c-f", 4)]
    assert len(reloads) == 1

    # Batch deleted removes matching known channels in one reload
    await events._apply_policy(  # pyright: ignore[reportPrivateUsage]
        kook,
        11,
        Observed(
            kind="kook.batch_deleted_channel",
            guild_id=GUILD_ON,
            data={"items": [{"id": CH_ON}, {"id": "missing"}]},
        ),
    )
    assert deleted == [CH_ON]
    assert len(reloads) == 2

    # Role & emoji change events immediately invalidate LiveKook caches
    await events._apply_policy(  # pyright: ignore[reportPrivateUsage]
        kook, 12, Observed(kind="kook.updated_role", guild_id=GUILD_ON, data={"role_id": 1})
    )
    await events._apply_policy(  # pyright: ignore[reportPrivateUsage]
        kook, 13, Observed(kind="kook.added_emoji", guild_id=GUILD_ON, data={"id": "e-1"})
    )
    assert kook.invalidated_meta == [GUILD_ON]
    assert kook.invalidated_emojis == [GUILD_ON]


async def test_dispatch_auto_discovers_unseen_channel_in_enabled_guild(
    snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    invoked: list[tuple[str | None, str]] = []
    synced: list[str] = []

    async def fake_sync(kook: FakeKook, channel_id: str, *, guild_id: str | None = None) -> Snapshot:
        synced.append(channel_id)
        ch = await kook.channel(channel_id)
        return snapshot(
            guilds={
                GUILD_ON: GuildInfo(GUILD_ON, "启用服", "", {}, enabled=True),
                GUILD_OFF: GuildInfo(GUILD_OFF, "停用服", "", {}, enabled=False),
            },
            channels={
                CAT_ON: ChannelInfo(CAT_ON, GUILD_ON, None, True, 0, "常规分组", True, {}),
                CH_ON: ChannelInfo(CH_ON, GUILD_ON, CAT_ON, False, 1, "闲聊", None, {}),
                ch.channel_id: ChannelInfo(
                    ch.channel_id, ch.guild_id, ch.parent_id, False, ch.level, ch.name, None, {}
                ),
            },
            triggers=(
                TriggerRule(1, "chat", "global", None, "mention", (), None, True, "quoted_or_self", 10, 0, ("member",)),
            ),
        )

    async def fake_invoke(_kook: Any, incoming: Any, resolved: Any) -> None:
        invoked.append((incoming.channel_id, resolved.handler))

    monkeypatch.setattr(events, "sync_channel", fake_sync)
    monkeypatch.setattr(router, "_invoke_handler", fake_invoke)
    monkeypatch.setattr(router.WRITER, "submit", lambda _msg: None)

    kook = FakeKook(
        bot_id=BOT,
        channel_map={
            GUILD_ON: [Channel(channel_id="ch-fresh", guild_id=GUILD_ON, name="刚建的频道", type=1, parent_id=CAT_ON)]
        },
    )
    # Message arrives in brand-new channel before manual dashboard sync -> auto-discovered & answered immediately!
    await router.dispatch(
        kook,
        make_incoming("你好", channel_id="ch-fresh", guild_id=GUILD_ON, user_id="u1", self_id=BOT, to_me=True),
    )
    assert synced == ["ch-fresh"]
    assert invoked == [("ch-fresh", "chat")]

    # Unknown channel in a disabled guild is NOT queried at all.
    await router.dispatch(
        kook,
        make_incoming("你好", channel_id="ch-off-new", guild_id=GUILD_OFF, user_id="u1", self_id=BOT, to_me=True),
    )
    assert synced == ["ch-fresh"]


async def test_deleted_channel_event_removes_config_and_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []
    reloads: list[str] = []
    published: list[str] = []

    async def fake_delete(channel_id: str) -> bool:
        deleted.append(channel_id)
        return True

    async def fake_load() -> Snapshot:
        reloads.append("load")
        return Snapshot()

    async def fake_publish(reason: str) -> None:
        published.append(reason)

    monkeypatch.setattr(events, "delete_channel_record", fake_delete)
    monkeypatch.setattr(events, "load", fake_load)
    monkeypatch.setattr(events, "publish_change", fake_publish)

    kook = FakeKook(bot_id=BOT)
    removed = Observed(kind="kook.deleted_channel", guild_id=GUILD_ON, channel_id=CH_ON, data={"id": CH_ON})
    await events._apply_policy(kook, 3, removed)  # pyright: ignore[reportPrivateUsage]
    assert deleted == [CH_ON]
    assert reloads == ["load"]
    assert published == [f"channel {CH_ON} deleted"]
