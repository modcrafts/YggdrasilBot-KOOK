"""KOOK sticker collection: guild custom emojis ``(emj)name(emj)[id]`` and chat GIF / small-image stickers.

- ``kind = 'emoji'``: KOOK custom guild emojis, keyed by ``emoji_id``. Their CDN image is fetched for vision tagging
  and cross-guild image fallback.
- ``kind = 'image'``: Standalone animated GIF/WebP or small sticker-like images (<= 512px max side, <= 5 MB) sent in
  chat, deduplicated by content sha256.
Usage is tracked per ``guild_id`` (guild-shared popularity).
"""

import asyncio
import hashlib
import io
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from yggdrasil.agent.media import download
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Sticker, StickerBlob, StickerUsage
from yggdrasil.db.session import get_session
from yggdrasil.kook import client as kook_client
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import KookError, emoji_origin_guild
from yggdrasil.kook.segments import Segment
from yggdrasil.tools.errors import ToolError

MAX_BYTES = 5 * 1024 * 1024
STICKER_IMAGE_MAX_SIDE = 512
STICKER_IMAGE_MAX_ASPECT = 2.2
TOO_LARGE = "too_large"
NOT_A_STICKER = "not_a_sticker"
_PERMANENT_ERRORS = frozenset({TOO_LARGE, NOT_A_STICKER})
_CONCURRENCY = 4

type UsageKey = tuple[str, str, str]  # (kind, ref, guild_id)


@dataclass(slots=True)
class Sighting:
    kind: str  # emoji | image
    ref: str
    summary: str
    guild_id: str | None = None
    emoji_id: str | None = None
    url: str = ""


@dataclass(frozen=True, slots=True)
class PendingFetch:
    sticker_id: int
    kind: str
    ref: str
    guild_id: str | None
    url: str


def sightings(
    segments: list[Segment], *, guild_id: str, allowed_guilds: Collection[str] | None = None
) -> tuple[list[Sighting], list[tuple[str, str, str]]]:
    """Extract immediate emoji sightings plus standalone image candidates ``(url, name, guild_id)``.

    When ``allowed_guilds`` is supplied, custom emojis whose origin guild is not in ``allowed_guilds`` are skipped.
    Multi-image messages are never treated as sticker candidates (usually screenshot/photo batches).
    """
    found: list[Sighting] = []
    img_candidates: list[tuple[str, str, str]] = []
    # Only treat a single standalone image (no long accompanying prose) as a sticker candidate.
    text_len = sum(len(str((s.get("data") or {}).get("text", "")).strip()) for s in segments if s.get("type") == "text")
    image_count = sum(1 for s in segments if s.get("type") == "image" and (s.get("data") or {}).get("url"))
    for seg in segments:
        data = seg.get("data") or {}
        st = seg.get("type")
        if st == "emoji" and data.get("id"):
            eid = str(data["id"])
            origin_gid = emoji_origin_guild(eid) or str(data.get("guild_id") or "") or guild_id
            if allowed_guilds is not None and origin_gid not in allowed_guilds:
                continue
            found.append(
                Sighting(
                    kind="emoji",
                    ref=eid,
                    summary=str(data.get("name") or eid),
                    guild_id=origin_gid or None,
                    emoji_id=eid,
                    url=str(data.get("url") or ""),
                )
            )
        elif st == "image" and data.get("url") and image_count == 1 and text_len <= 10:
            url = str(data["url"])
            name = str(data.get("name") or "")
            img_candidates.append((url, name, guild_id))
    return found, img_candidates


async def enrich_emoji_sightings(
    seen: dict[tuple[str, str], Sighting],
    usage: Counter[UsageKey],
) -> None:
    """Resolve true origin ``guild_id`` and CDN URL for custom emoji sightings.

    - Joined-guild emojis get their authoritative ``guild_id`` and exact ``.png``/``.gif`` CDN URL from KOOK.
    - Foreign-guild emojis (bot not in the origin guild) are still collected so their CDN image can be cached in
      ``sticker_blobs`` and sent as regular image/GIF stickers; their ``guild_id`` is strictly set to the emoji's
      true origin prefix (or ``None`` if unprefixed) so senders never mistake them for native same-guild ``(emj)``.
    """
    if not seen:
        return
    kook = kook_client.connected()
    for (kind, ref), s in seen.items():
        if kind != "emoji":
            continue
        origin_gid = emoji_origin_guild(ref)
        if kook is not None:
            hint_gid = origin_gid or s.guild_id or next((g for k, r, g in usage if k == kind and r == ref and g), None)
            try:
                resolved = await kook.resolve_emoji(ref, hint_guild_id=hint_gid)
            except KookError as e:
                LOGGER.debug("emoji {} lookup failed ({}); keeping origin guild {}", ref, e, origin_gid)
                if origin_gid is not None:
                    s.guild_id = origin_gid
            else:
                if resolved is not None:
                    s.guild_id = resolved.guild_id or origin_gid
                    if not s.url:
                        s.url = resolved.url
                else:
                    # Confirmed not in any joined guild: never leave the channel's guild_id on a foreign emoji.
                    s.guild_id = origin_gid
        elif origin_gid is not None:
            s.guild_id = origin_gid


async def repair_emoji_stickers(kook: Kook) -> int:
    """Repair misattributed ``guild_id`` on stored ``kind='emoji'`` rows using KOOK's joined guild emoji lists.

    Previously, foreign emojis sent inside a guild were stored with the receiving channel's ``guild_id``, causing
    senders to emit unrenderable native ``(emj)`` tokens instead of falling back to their cached ``StickerBlob``
    image. Updating ``guild_id`` to the true origin guild (or ``None`` when unprefixed and unjoined) makes those
    existing foreign emojis send cleanly as images.
    """
    try:
        joined = await kook.joined_guild_ids()
    except KookError as e:
        LOGGER.debug("skipping emoji guild_id repair: failed to list joined guilds ({})", e)
        return 0
    if not joined:
        return 0
    repaired = 0
    async with get_session() as session:
        rows = (await session.scalars(select(Sticker).where(Sticker.kind == "emoji"))).all()
        for st in rows:
            eid = st.emoji_id or st.ref
            try:
                resolved = await kook.resolve_emoji(eid, hint_guild_id=st.guild_id)
            except KookError as e:
                LOGGER.debug("skipping emoji {} during repair ({})", eid, e)
                continue
            target_gid = resolved.guild_id if resolved is not None else emoji_origin_guild(eid)
            if st.guild_id != target_gid:
                st.guild_id = target_gid
                repaired += 1
    if repaired:
        LOGGER.info("emoji sticker sync: repaired guild_id on {} stored custom emojis", repaired)
    return repaired


async def record(
    session: AsyncSession, seen: dict[tuple[str, str], Sighting], usage: Counter[UsageKey]
) -> list[PendingFetch]:
    """Upsert stickers and per-guild usage counts; returns items whose image bytes still need downloading."""
    if not seen:
        return []
    counts = Counter[tuple[str, str]]()
    for (kind, ref, _guild), n in usage.items():
        counts[kind, ref] += n
    stmt = insert(Sticker).values(
        [
            {
                "kind": s.kind,
                "ref": s.ref,
                "guild_id": s.guild_id,
                "emoji_id": s.emoji_id,
                "summary": s.summary,
                "status": "fetching",
                "seen_count": counts[k],
            }
            for k, s in seen.items()
        ]
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=["kind", "ref"],
        set_={
            "seen_count": Sticker.seen_count + stmt.excluded.seen_count,
            "last_seen": func.now(),
            "summary": stmt.excluded.summary,
            "guild_id": stmt.excluded.guild_id,
        },
    ).returning(Sticker.id, Sticker.kind, Sticker.ref, Sticker.status, Sticker.error)
    rows = (await session.execute(upsert)).all()
    ids = {(kind, ref): sid for sid, kind, ref, _, _ in rows}
    if usage:
        usage_stmt = insert(StickerUsage).values(
            [{"sticker_id": ids[kind, ref], "guild_id": g, "count": n} for (kind, ref, g), n in usage.items()]
        )
        await session.execute(
            usage_stmt.on_conflict_do_update(
                index_elements=["sticker_id", "guild_id"],
                set_={"count": StickerUsage.count + usage_stmt.excluded.count, "last_seen": func.now()},
            )
        )
    pending: list[PendingFetch] = []
    for sid, kind, ref, status, error in rows:
        if status == "fetching" or (status == "failed" and error not in _PERMANENT_ERRORS):
            s = seen[kind, ref]
            pending.append(PendingFetch(sid, kind, ref, s.guild_id, s.url))
    return pending


def _inspect_image(data: bytes) -> tuple[str, int, int, int, bool]:
    """Return ``(mime, width, height, frames, is_sticker_like)``."""
    with Image.open(io.BytesIO(data)) as img:
        w, h = img.size
        frames = int(getattr(img, "n_frames", 1) or 1)
        fmt = (img.format or "PNG").lower()
        mime = f"image/{'jpeg' if fmt == 'jpg' else fmt}"
        animated = frames > 1
        short_side, long_side = min(w, h), max(w, h)
        # Collect animated GIF/WebP or compact, roughly square sticker-sized images (reject thin screenshot strips).
        is_sticker = animated or (
            long_side <= STICKER_IMAGE_MAX_SIDE
            and short_side >= 32
            and (long_side / short_side) <= STICKER_IMAGE_MAX_ASPECT
        )
        return mime, w, h, frames, is_sticker


@dataclass(frozen=True, slots=True)
class SavedSticker:
    sticker_id: int
    status: str  # ready | untagged
    existed: bool
    restored: bool
    frames: int
    description: str


async def save_image_sticker(
    data: bytes,
    *,
    guild_id: str | None,
    name: str = "",
    description: str = "",
    usage: str = "",
    emotions: list[str] | None = None,
    ocr_text: str = "",
) -> SavedSticker:
    """Explicitly save an image/GIF from conversation into the sticker library (deduplicated by sha256).

    Unlike passive auto-collection, this bypasses the ``<= 512px`` / aspect-ratio heuristic because the model or user
    deliberately chose this image. Refuses images previously flagged unsafe or manually disabled by an admin, while
    allowing recovery of images previously auto-rejected as ``not_a_sticker``.
    """
    if len(data) > MAX_BYTES:
        raise ToolError(f"图片超过 {MAX_BYTES // (1024 * 1024)} MB 上限，无法录入表情包库")
    try:
        mime, w, h, frames, _ = await asyncio.to_thread(_inspect_image, data)
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        raise ToolError("这个文件不是有效的图片（支持 PNG、JPEG、GIF、WebP 等）") from e

    sha = hashlib.sha256(data).hexdigest()
    desc = description.strip()
    use = usage.strip()
    ocr = ocr_text.strip()
    emos = [e.strip() for e in (emotions or ()) if e.strip()][:4]
    summary = (name or desc or "sticker").strip()[:80]
    has_full_tags = bool(desc and use)
    target_status = "ready" if has_full_tags else "untagged"
    now = datetime.now(UTC)

    async with get_session() as session:
        existing = await session.scalar(select(Sticker).where(Sticker.kind == "image", Sticker.ref == sha))
        if existing is not None:
            if existing.unsafe_category:
                raise ToolError(f"这张图此前因安全原因被标记（{existing.unsafe_category}），不能录入表情包库")
            if not existing.enabled and existing.error != NOT_A_STICKER:
                raise ToolError(f"这张图已在库中（#{existing.id}）但被管理员禁用了，如需恢复请在控制台启用")
            restored = existing.status == "failed" or not existing.enabled
            existing.seen_count += 1
            existing.last_seen = now
            if restored:
                existing.enabled = True
                existing.error = ""
                existing.status = target_status
                if desc:
                    existing.description = desc
                if use:
                    existing.usage = use
                if emos:
                    existing.emotions = emos
                if ocr:
                    existing.ocr_text = ocr
                if has_full_tags:
                    existing.tagged_at = now
            else:
                if desc and not existing.description:
                    existing.description = desc
                if use and not existing.usage:
                    existing.usage = use
                if emos and not existing.emotions:
                    existing.emotions = emos
                if ocr and not existing.ocr_text:
                    existing.ocr_text = ocr
                if existing.status != "ready" and existing.description and existing.usage:
                    existing.status = "ready"
                    existing.tagged_at = now
            sid = existing.id
            final_status = existing.status
            final_desc = existing.description or existing.summary
            existed = not restored
        else:
            row = Sticker(
                kind="image",
                ref=sha,
                guild_id=guild_id or None,
                summary=summary,
                status=target_status,
                mime=mime,
                size_bytes=len(data),
                width=w,
                height=h,
                frames=frames,
                description=desc,
                usage=use,
                ocr_text=ocr,
                emotions=emos,
                tagged_at=now if has_full_tags else None,
                seen_count=1,
                enabled=True,
            )
            session.add(row)
            await session.flush()
            sid = row.id
            final_status = row.status
            final_desc = row.description or row.summary
            existed = False
            restored = False

        await session.execute(
            insert(StickerBlob).values(sticker_id=sid, data=data).on_conflict_do_nothing(index_elements=["sticker_id"])
        )
        if guild_id:
            u_stmt = insert(StickerUsage).values(sticker_id=sid, guild_id=guild_id, count=1)
            await session.execute(
                u_stmt.on_conflict_do_update(
                    index_elements=["sticker_id", "guild_id"],
                    set_={"count": StickerUsage.count + 1, "last_seen": func.now()},
                )
            )
    return SavedSticker(
        sticker_id=sid,
        status=final_status,
        existed=existed,
        restored=restored,
        frames=frames,
        description=final_desc,
    )


class StickerCollector:
    """Background downloader for KOOK custom emojis and sticker-like images."""

    def __init__(self) -> None:
        self._sem = asyncio.Semaphore(_CONCURRENCY)
        self._inflight: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    def schedule(self, items: list[PendingFetch]) -> None:
        for item in items:
            key = f"id:{item.sticker_id}"
            if key in self._inflight:
                continue
            self._inflight.add(key)
            self._spawn(self._fetch_pending(item, key))

    def schedule_images(self, candidates: list[tuple[str, str, str]]) -> None:
        for url, name, guild_id in candidates:
            key = f"url:{url}"
            if key in self._inflight:
                continue
            self._inflight.add(key)
            self._spawn(self._inspect_and_collect_image(url, name, guild_id, key))

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro, name="sticker-collect")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch_pending(self, item: PendingFetch, key: str) -> None:
        try:
            async with self._sem:
                url = item.url
                if not url and item.kind == "emoji":
                    kook = kook_client.connected()
                    if kook is not None:
                        try:
                            resolved = await kook.resolve_emoji(item.ref, hint_guild_id=item.guild_id)
                        except KookError:
                            resolved = None
                        if resolved is not None:
                            url = resolved.url
                    if not url:
                        url = f"https://img.kookapp.cn/emojis/{item.ref}.png"
                if not url:
                    return
                try:
                    data = await download(url, MAX_BYTES)
                except ToolError:
                    await self._mark_failed(item.sticker_id, TOO_LARGE)
                    return
                except Exception as e:
                    # Try .gif fallback for KOOK animated custom emojis when .png 404s.
                    if item.kind == "emoji" and url.endswith(".png"):
                        try:
                            data = await download(url[:-4] + ".gif", MAX_BYTES)
                        except Exception:
                            await self._mark_failed(item.sticker_id, f"fetch: {e}"[:300])
                            return
                    else:
                        await self._mark_failed(item.sticker_id, f"fetch: {e}"[:300])
                        return
                mime, w, h, frames, _ = await asyncio.to_thread(_inspect_image, data)
                async with get_session() as session:
                    await session.execute(
                        insert(StickerBlob)
                        .values(sticker_id=item.sticker_id, data=data)
                        .on_conflict_do_nothing(index_elements=["sticker_id"])
                    )
                    await session.execute(
                        update(Sticker)
                        .where(Sticker.id == item.sticker_id)
                        .values(
                            status="untagged",
                            error="",
                            mime=mime,
                            size_bytes=len(data),
                            width=w,
                            height=h,
                            frames=frames,
                        )
                    )
        except Exception:
            LOGGER.exception("collecting sticker {} failed", item.sticker_id)
        finally:
            self._inflight.discard(key)

    async def _inspect_and_collect_image(self, url: str, name: str, guild_id: str, key: str) -> None:
        try:
            async with self._sem:
                try:
                    data = await download(url, MAX_BYTES)
                    mime, w, h, frames, is_sticker = await asyncio.to_thread(_inspect_image, data)
                except Exception:
                    return
                if not is_sticker:
                    return
                sha = hashlib.sha256(data).hexdigest()
                async with get_session() as session:
                    stmt = insert(Sticker).values(
                        kind="image",
                        ref=sha,
                        guild_id=guild_id,
                        summary=name[:80],
                        status="untagged",
                        mime=mime,
                        size_bytes=len(data),
                        width=w,
                        height=h,
                        frames=frames,
                        seen_count=1,
                    )
                    row = (
                        await session.execute(
                            stmt.on_conflict_do_update(
                                index_elements=["kind", "ref"],
                                set_={"seen_count": Sticker.seen_count + 1, "last_seen": func.now()},
                            ).returning(Sticker.id, Sticker.status, Sticker.error)
                        )
                    ).first()
                    if row is None:
                        return
                    sid, status, error = row
                    # Previously rejected as non-sticker (e.g. screenshot): keep dedup row without re-storing blob.
                    if status == "failed" and error in _PERMANENT_ERRORS:
                        return
                    await session.execute(
                        insert(StickerBlob)
                        .values(sticker_id=sid, data=data)
                        .on_conflict_do_nothing(index_elements=["sticker_id"])
                    )
                    u_stmt = insert(StickerUsage).values(sticker_id=sid, guild_id=guild_id, count=1)
                    await session.execute(
                        u_stmt.on_conflict_do_update(
                            index_elements=["sticker_id", "guild_id"],
                            set_={"count": StickerUsage.count + 1, "last_seen": func.now()},
                        )
                    )
        except Exception:
            LOGGER.debug("image sticker inspection failed for {}", url)
        finally:
            self._inflight.discard(key)

    async def _mark_failed(self, sticker_id: int, reason: str) -> None:
        async with get_session() as session:
            await session.execute(update(Sticker).where(Sticker.id == sticker_id).values(status="failed", error=reason))


COLLECTOR = StickerCollector()
