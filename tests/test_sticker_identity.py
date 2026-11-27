"""KOOK sticker identification: guild custom emojis ``(emj)``, foreign emoji filtering, and screenshot rejection."""

import io
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any

import pytest
from PIL import Image

from tests.support.kook import FakeKook
from yggdrasil.kook import client as kook_client
from yggdrasil.kook.model import Guild, GuildEmoji, KookError, emoji_origin_guild
from yggdrasil.kook.parser import parse_kmarkdown
from yggdrasil.stickers import tagging
from yggdrasil.stickers.collect import (
    NOT_A_STICKER,
    Sighting,
    _inspect_image,
    enrich_emoji_sightings,
    sightings,
)


def _make_png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 180, 240)).save(buf, format="PNG")
    return buf.getvalue()


def _make_gif(w: int, h: int, frames: int = 2) -> bytes:
    buf = io.BytesIO()
    imgs = [Image.new("RGB", (w, h), (i * 50, 100, 150)) for i in range(frames)]
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=100, loop=0)
    return buf.getvalue()


def test_emoji_origin_guild_and_kmarkdown_preserve_real_origin_guild():
    assert emoji_origin_guild("1591057729615250/9nG5PxHkZE074074") == "1591057729615250"
    assert emoji_origin_guild("g-other/e1") == "g-other"
    assert emoji_origin_guild("https://img.kookapp.cn/emojis/g1/e1.png") is None
    assert emoji_origin_guild("plain-no-slash") is None
    assert emoji_origin_guild("") is None
    assert emoji_origin_guild(None) is None

    # Foreign emoji sent inside guild g-1 records its true origin guild g-other, NOT g-1.
    segs = parse_kmarkdown("你好 (emj)hi(emj)[g-other/emo2] 和 (emj)ok(emj)[g-1/emo1]", guild_id="g-1")
    emjs = [s["data"] for s in segs if s["type"] == "emoji"]
    assert emjs == [
        {"id": "g-other/emo2", "name": "hi", "guild_id": "g-other"},
        {"id": "g-1/emo1", "name": "ok", "guild_id": "g-1"},
    ]


def test_kook_guild_emojis_and_standalone_gif_images_are_identified():
    url = "https://img.kookapp.cn/emojis/g1/e1.png"
    emoji_segs = [{"type": "emoji", "data": {"id": "g1/e1", "name": "doge", "guild_id": "g1", "url": url}}]
    found, img_cands = sightings(emoji_segs, guild_id="g1")
    assert found == [Sighting("emoji", "g1/e1", "doge", url=url, emoji_id="g1/e1", guild_id="g1")]
    assert img_cands == []

    # allowed_guilds filters out foreign emojis even when data["guild_id"] was stamped with the channel guild.
    foreign_segs = [{"type": "emoji", "data": {"id": "g-unjoined/e9", "name": "ext", "guild_id": "g1"}}]
    found_filtered, _ = sightings(foreign_segs, guild_id="g1", allowed_guilds={"g1"})
    assert found_filtered == []

    # Standalone GIF image is a sticker candidate
    gif_segs = [{"type": "image", "data": {"url": "https://img.kookapp.cn/assets/cat.gif", "name": "cat.gif"}}]
    found2, img_cands2 = sightings(gif_segs, guild_id="g1")
    assert found2 == []
    assert img_cands2 == [("https://img.kookapp.cn/assets/cat.gif", "cat.gif", "g1")]

    # Multi-image batch is treated as shared screenshots/photos, not sticker candidates
    multi_img_segs = [
        {"type": "image", "data": {"url": "https://img.kookapp.cn/assets/s1.png", "name": "s1.png"}},
        {"type": "image", "data": {"url": "https://img.kookapp.cn/assets/s2.png", "name": "s2.png"}},
    ]
    _, img_cands_multi = sightings(multi_img_segs, guild_id="g1")
    assert img_cands_multi == []

    # Image accompanied by long text is treated as a regular chat photo, not a sticker
    photo_segs = [
        {"type": "text", "data": {"text": "这是我今天拍的风景照大家看看"}},
        {"type": "image", "data": {"url": "https://img.kookapp.cn/assets/photo.png", "name": "photo.png"}},
    ]
    found3, img_cands3 = sightings(photo_segs, guild_id="g1")
    assert found3 == []
    assert img_cands3 == []


def test_inspect_image_rejects_thin_screenshot_strips_and_keeps_stickers():
    # Normal square sticker image passes pre-filter
    _, w, h, frames, is_sticker = _inspect_image(_make_png(256, 256))
    assert (w, h, frames, is_sticker) == (256, 256, 1, True)

    # Wide chat/error screenshot strip (aspect 4.0 > 2.2) is rejected
    assert _inspect_image(_make_png(480, 120))[4] is False

    # Tall menu/log screenshot strip (aspect 4.0 > 2.2) is rejected
    assert _inspect_image(_make_png(100, 400))[4] is False

    # Oversized full screenshot (> 512px) is rejected
    assert _inspect_image(_make_png(800, 600))[4] is False

    # Animated GIF still passes even with wide aspect
    _, _, _, gif_frames, gif_ok = _inspect_image(_make_gif(320, 120, frames=2))
    assert gif_frames == 2
    assert gif_ok is True


async def test_enrich_emoji_sightings_keeps_foreign_emojis_with_true_origin_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeKook(
        guild_list=[Guild(guild_id="g1", name="Main"), Guild(guild_id="g2", name="Alt")],
        emoji_map={
            "g1": [GuildEmoji(id="g1/e1", name="doge", emoji_type=0, guild_id="g1")],
            "g2": [GuildEmoji(id="g2/e2", name="party", emoji_type=1, guild_id="g2")],
        },
    )
    monkeypatch.setattr(kook_client, "_active", fake)

    seen = {
        ("emoji", "g1/e1"): Sighting("emoji", "g1/e1", "doge", guild_id="g1", emoji_id="g1/e1"),
        ("emoji", "g2/e2"): Sighting("emoji", "g2/e2", "party", guild_id="g1", emoji_id="g2/e2"),
        ("emoji", "g9/e9"): Sighting("emoji", "g9/e9", "foreign", guild_id="g1", emoji_id="g9/e9"),
        ("emoji", "unprefixed"): Sighting("emoji", "unprefixed", "store", guild_id="g1", emoji_id="unprefixed"),
    }
    usage = Counter(
        {
            ("emoji", "g1/e1", "g1"): 2,
            ("emoji", "g2/e2", "g1"): 1,
            ("emoji", "g9/e9", "g1"): 3,
            ("emoji", "unprefixed", "g1"): 1,
        }
    )

    await enrich_emoji_sightings(seen, usage)
    # Joined same-guild emoji: gets authoritative guild_id and .png URL
    assert seen["emoji", "g1/e1"].guild_id == "g1"
    assert seen["emoji", "g1/e1"].url.endswith("g1/e1.png")
    # Joined other-guild emoji: gets authoritative guild_id and .gif URL
    assert seen["emoji", "g2/e2"].guild_id == "g2"
    assert seen["emoji", "g2/e2"].url.endswith("g2/e2.gif")
    # Unjoined foreign-guild emoji: kept for CDN image fallback, stamped with real origin guild g9 (NOT g1!)
    assert seen["emoji", "g9/e9"].guild_id == "g9"
    # Unprefixed emoji not in any joined guild: guild_id cleared to None so it never sends as native (emj)
    assert seen["emoji", "unprefixed"].guild_id is None


async def test_enrich_emoji_sightings_preserves_origin_guild_on_kook_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenKook(FakeKook):
        async def joined_guild_ids(self) -> frozenset[str]:
            raise KookError("temporary error")

    monkeypatch.setattr(kook_client, "_active", BrokenKook())
    seen = {
        ("emoji", "g1/e1"): Sighting("emoji", "g1/e1", "doge", guild_id="g1", emoji_id="g1/e1"),
        ("emoji", "g9/e9"): Sighting("emoji", "g9/e9", "foreign", guild_id="g1", emoji_id="g9/e9"),
    }
    usage = Counter({("emoji", "g1/e1", "g1"): 1, ("emoji", "g9/e9", "g1"): 1})
    await enrich_emoji_sightings(seen, usage)
    assert seen["emoji", "g1/e1"].guild_id == "g1"
    assert seen["emoji", "g9/e9"].guild_id == "g9"


async def test_tagging_rejects_non_sticker_image_and_drops_blob_but_keeps_guild_emoji(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[Any] = []

    class FakeSession:
        async def execute(self, stmt: Any) -> None:
            executed.append(stmt)

    @asynccontextmanager
    async def _fake_session():
        yield FakeSession()

    monkeypatch.setattr(tagging, "get_session", _fake_session)

    screenshot_tags = tagging.StickerTags(
        is_sticker=False,
        description="游戏报错弹窗截图",
        ocr_text="NullPointerException",
        emotions=[],
        usage="",
        safe=True,
    )
    rejected = await tagging._save(42, "image", screenshot_tags)  # pyright: ignore[reportPrivateUsage]
    assert rejected is True
    assert len(executed) == 2  # update stickers + delete sticker_blobs
    upd_params = executed[0].compile().params
    assert upd_params["status"] == "failed"
    assert upd_params["error"] == NOT_A_STICKER
    assert upd_params["enabled"] is False
    assert "DELETE FROM sticker_blobs" in str(executed[1])

    # Custom guild emoji is never rejected even if is_sticker=False
    executed.clear()
    rejected_emoji = await tagging._save(43, "emoji", screenshot_tags)  # pyright: ignore[reportPrivateUsage]
    assert rejected_emoji is False
    assert len(executed) == 1
    assert executed[0].compile().params["status"] == "ready"
