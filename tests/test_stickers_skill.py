"""Tests for the ``stickers`` skill (inline KOOK emojis, image stickers, and ``save_sticker``)."""

from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.support.agent import run_ctx
from tests.support.files import png
from tests.support.kook import FakeKook
from yggdrasil.agent.schema import BotReply, Section
from yggdrasil.agent.skills import SKILLS, skill_names, stickers
from yggdrasil.agent.tools.images import reply_images
from yggdrasil.db.models import Sticker
from yggdrasil.moderation.image import ImageVerdict
from yggdrasil.settings import Settings
from yggdrasil.stickers import collect as sticker_collect
from yggdrasil.stickers.collect import NOT_A_STICKER, SavedSticker, save_image_sticker
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.notes import blob_id

GUILD, CHANNEL, SELF, ALICE = "g-1", "ch-1", "10001", "20001"
TINY_GIF = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
    b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


def test_stickers_skill_is_registered_and_enabled_by_default() -> None:
    assert "stickers" in SKILLS
    assert "stickers" in skill_names(Settings(), "chat", CHANNEL)
    assert skill_names(Settings(), "tldr", CHANNEL) == []


async def test_search_stickers_filters_and_ranks_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = [
        stickers._StickerRow(  # pyright: ignore[reportPrivateUsage]
            id=1,
            kind="emoji",
            guild_id=GUILD,
            emoji_id="g-1/emj-doge",
            summary="doge",
            description="滑稽狗头",
            usage="调侃和吐槽",
            emotions=["滑稽", "吐槽"],
            ocr_text="",
            frames=1,
            uses=20,
            seen_count=20,
        ),
        stickers._StickerRow(  # pyright: ignore[reportPrivateUsage]
            id=2,
            kind="image",
            guild_id=None,
            emoji_id=None,
            summary="cat.gif",
            description="猫咪疯狂敲键盘",
            usage="写代码崩溃或赶工",
            emotions=["崩溃", "忙碌"],
            ocr_text="改不完了",
            frames=12,
            uses=5,
            seen_count=10,
        ),
    ]

    async def _fake_pool(_guild_id: str | None) -> list[stickers._StickerRow]:  # pyright: ignore[reportPrivateUsage]
        return pool

    monkeypatch.setattr(stickers, "_load_pool", _fake_pool)
    ctx = run_ctx(kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=Settings())

    hit_emoji = str(await stickers.search_stickers(ctx, "doge"))
    assert "#1 [本服内联表情] doge：滑稽狗头" in hit_emoji
    assert "调侃和吐槽" in hit_emoji
    assert "#2" not in hit_emoji

    hit_gif = str(await stickers.search_stickers(ctx, "崩溃"))
    assert "#2 [动图表情包]" in hit_gif
    assert "#1" not in hit_gif

    miss = str(await stickers.search_stickers(ctx, "完全不存在的关键词"))
    assert "没有找到匹配" in miss


async def test_pick_sticker_inline_emoji_and_animated_image_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    same_emoji = Sticker(
        id=1,
        kind="emoji",
        ref="g-1/emj-doge",
        guild_id=GUILD,
        emoji_id="g-1/emj-doge",
        summary="doge",
        status="ready",
        enabled=True,
    )
    gif_sticker = Sticker(
        id=2,
        kind="image",
        ref="sha-gif",
        guild_id=None,
        emoji_id=None,
        summary="cat.gif",
        status="ready",
        enabled=True,
    )
    cross_emoji = Sticker(
        id=3,
        kind="emoji",
        ref="g-other/emj-hi",
        guild_id="g-other",
        emoji_id="g-other/emj-hi",
        summary="hi",
        status="ready",
        enabled=True,
    )
    png_bytes = png("blue")
    store: dict[int, tuple[Sticker, bytes | None]] = {
        1: (same_emoji, None),
        2: (gif_sticker, TINY_GIF),
        3: (cross_emoji, png_bytes),
    }

    async def _fake_load(sid: int) -> tuple[Sticker | None, bytes | None]:
        return store.get(sid, (None, None))

    monkeypatch.setattr(stickers, "_load_sticker", _fake_load)
    ctx = run_ctx(kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=Settings())

    # 1) Same-guild KOOK custom emoji returns native inline KMarkdown token
    res_emoji = str(await stickers.pick_sticker(ctx, 1))
    assert "(emj)doge(emj)[g-1/emj-doge]" in res_emoji

    # 2) Image/GIF sticker registers img#1 and preserves original GIF bytes in reply_images
    res_gif = str(await stickers.pick_sticker(ctx, 2))
    assert "img#1" in res_gif
    reply = BotReply(mode="reply", style="text", sections=[Section(content="", images=["img#1"])])
    pics = await reply_images(ctx.deps, reply)
    assert pics["img#1"][0] == TINY_GIF

    # 3) Cross-guild custom emoji falls back to image handle instead of unusable (emj) token
    res_cross = str(await stickers.pick_sticker(ctx, 3))
    assert "img#2" in res_cross
    assert "(emj)" not in res_cross

    # 4) Stale polluted DB row (guild_id falsely stamped as current GUILD, but emoji_id prefix is g-other)
    #    must still fall back to image handle and never emit an unrenderable (emj) token.
    polluted_emoji = Sticker(
        id=4,
        kind="emoji",
        ref="g-other/emj-polluted",
        guild_id=GUILD,
        emoji_id="g-other/emj-polluted",
        summary="polluted",
        status="ready",
        enabled=True,
    )
    store[4] = (polluted_emoji, png("red"))
    res_polluted = str(await stickers.pick_sticker(ctx, 4))
    assert "img#3" in res_polluted
    assert "(emj)" not in res_polluted


async def test_save_sticker_tool_handles_new_duplicate_moderation_and_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saved_calls: list[dict[str, Any]] = []

    async def _fake_save(data: bytes, **kwargs: Any) -> SavedSticker:
        saved_calls.append({"data": data, **kwargs})
        return SavedSticker(
            sticker_id=77,
            status="ready" if kwargs.get("description") and kwargs.get("usage") else "untagged",
            existed=False,
            restored=False,
            frames=1,
            description=str(kwargs.get("description") or "meme"),
        )

    monkeypatch.setattr(stickers, "save_image_sticker", _fake_save)

    settings = Settings()
    settings.moderation.enabled = False
    ctx = run_ctx(kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=settings)
    img_bytes = png("green")
    sha = blob_id(img_bytes)
    ctx.deps.made_images[sha] = img_bytes
    key = ctx.deps.handles.add("image", {"blob": sha, "caption": "meme.png"})
    assert key == "img#1"

    # 1) Save with description & usage -> immediately ready and recorded in deps.actions
    out = str(
        await stickers.save_sticker(
            ctx,
            "img#1",
            description="猫猫举手赞成",
            usage="表示强烈赞同",
            emotions=["赞同", "可爱"],
        )
    )
    assert "#77" in out
    assert "现已可被检索和选用" in out
    assert "save_sticker:#77" in ctx.deps.actions
    assert saved_calls[0]["guild_id"] == GUILD
    assert saved_calls[0]["data"] == img_bytes

    # 2) Moderation blocks unsafe image before save_image_sticker is called
    mod_settings = Settings()
    mod_settings.moderation.enabled = True
    mod_settings.moderation.check_output = True

    async def _unsafe(_blob: bytes) -> ImageVerdict:
        return ImageVerdict(False, "gore", "vision-m", 10, 5)

    monkeypatch.setattr(stickers, "check_image", _unsafe)
    ctx_mod = run_ctx(
        kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=mod_settings
    )
    ctx_mod.deps.made_images[sha] = img_bytes
    ctx_mod.deps.handles.add("image", {"blob": sha, "caption": "bad.png"})
    out_unsafe = str(await stickers.save_sticker(ctx_mod, "img#1"))
    assert "审核未通过" in out_unsafe
    assert len(saved_calls) == 1

    # 3) Respects features.sticker_collect = False
    off_settings = Settings()
    off_settings.features.sticker_collect = False
    ctx_off = run_ctx(
        kook=FakeKook(bot_id=SELF), channel_id=CHANNEL, guild_id=GUILD, user_id=ALICE, settings=off_settings
    )
    out_off = str(await stickers.save_sticker(ctx_off, "img#1"))
    assert "未开启表情包收集" in out_off


async def test_save_image_sticker_validates_restores_and_blocks_admin_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_row: Sticker | None = None
    added: list[Sticker] = []
    executed: list[Any] = []

    class FakeSession:
        async def scalar(self, _stmt: Any) -> Sticker | None:
            return existing_row

        def add(self, obj: Sticker) -> None:
            obj.id = 101
            added.append(obj)

        async def flush(self) -> None:
            pass

        async def execute(self, stmt: Any) -> None:
            executed.append(stmt)

    @asynccontextmanager
    async def _fake_session():
        yield FakeSession()

    monkeypatch.setattr(sticker_collect, "get_session", _fake_session)

    # Non-image bytes rejected
    with pytest.raises(ToolError, match="不是有效的图片"):
        await save_image_sticker(b"not an image", guild_id=GUILD)

    # Fresh image with full tags -> saved as ready
    res_new = await save_image_sticker(
        png("blue"), guild_id=GUILD, description="蓝色表情", usage="测试", emotions=["开心"]
    )
    assert res_new.sticker_id == 101
    assert res_new.status == "ready"
    assert res_new.existed is False
    assert added[0].status == "ready"

    # Previously auto-rejected (not_a_sticker) -> explicitly restored by save_image_sticker
    existing_row = Sticker(
        id=55,
        kind="image",
        ref="sha-old",
        guild_id=GUILD,
        summary="shot.png",
        status="failed",
        error=NOT_A_STICKER,
        enabled=False,
        seen_count=1,
        unsafe_category="",
        description="",
        usage="",
        ocr_text="",
        emotions=[],
    )
    res_restored = await save_image_sticker(png("blue"), guild_id=GUILD, description="其实是个梗图", usage="玩梗")
    assert res_restored.sticker_id == 55
    assert res_restored.restored is True
    assert res_restored.status == "ready"
    assert existing_row.enabled is True
    assert existing_row.error == ""

    # Manually disabled by admin -> refused
    existing_row = Sticker(
        id=56,
        kind="image",
        ref="sha-disabled",
        guild_id=GUILD,
        summary="no.png",
        status="ready",
        error="",
        enabled=False,
        seen_count=2,
        unsafe_category="",
    )
    with pytest.raises(ToolError, match="被管理员禁用"):
        await save_image_sticker(png("blue"), guild_id=GUILD)

    # Previously flagged unsafe -> refused
    existing_row = Sticker(
        id=57,
        kind="image",
        ref="sha-unsafe",
        guild_id=GUILD,
        summary="bad.png",
        status="ready",
        error="",
        enabled=False,
        seen_count=1,
        unsafe_category="gore",
    )
    with pytest.raises(ToolError, match="安全原因"):
        await save_image_sticker(png("blue"), guild_id=GUILD)
