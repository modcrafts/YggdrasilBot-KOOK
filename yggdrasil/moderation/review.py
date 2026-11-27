"""Held replies: stored as ReviewItem, sent to superusers as KOOK direct CardMessages, decided by them."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from yggdrasil.agent.schema import BotReply
from yggdrasil.bot.state import identity
from yggdrasil.core.log import LOGGER
from yggdrasil.db.models import Invocation, ReviewItem
from yggdrasil.db.session import get_session
from yggdrasil.delivery.render import build_notice_card
from yggdrasil.delivery.send import send_kmarkdown, send_reply
from yggdrasil.kook.client import Kook, connected
from yggdrasil.moderation.guard import Verdict
from yggdrasil.people import member_label
from yggdrasil.runtime import snapshot

pending_count = 0


class ReviewError(Exception):
    pass


def parse_review_id(raw: str) -> int:
    raw = raw.strip().lstrip("#").upper().removeprefix("R")
    if not raw.isdigit():
        raise ReviewError(f"无效的审核编号：{raw}")
    return int(raw)


async def notify_admins(kook: Kook, text: str) -> None:
    for uid in snapshot().superusers:
        try:
            await kook.send_kmarkdown(channel_id=None, user_id=uid, content=text)
        except Exception as e:
            LOGGER.warning("notify admin {} failed: {}", uid, e)


async def create_review(
    kook: Kook,
    *,
    invocation_id: UUID | None,
    channel_id: str,
    guild_id: str | None,
    user_id: str,
    trigger_msg_id: str | None,
    request_text: str,
    reply: BotReply,
    verdict: Verdict,
    expire_minutes: int,
) -> int:
    async with get_session() as session:
        item = ReviewItem(
            invocation_id=invocation_id,
            channel_id=channel_id,
            guild_id=guild_id,
            user_id=user_id,
            trigger_msg_id=trigger_msg_id,
            request_text=request_text[:4000],
            reply=reply.model_dump(),
            verdict=verdict.as_dict(),
            expires_at=datetime.now(UTC) + timedelta(minutes=expire_minutes),
        )
        session.add(item)
        await session.flush()
        review_id = item.id

    snap = snapshot()
    ch_name = snap.channel_name(channel_id) or channel_id
    g_name = snap.guild_name(guild_id) or guild_id or ""
    who = await member_label(guild_id, user_id)
    summary = (
        f"**服务器 / 频道**：{g_name} / #{ch_name} (`{channel_id}`)\n"
        f"**发起人**：{who}\n"
        f"**判定**：{verdict.verdict} {verdict.score:.2f} {'、'.join(verdict.categories) or ''}"
    )
    card_json = build_notice_card(
        title=f"待审核 R{review_id}",
        sections=[
            summary,
            f"**请求内容**\n{request_text[:1500]}",
            f"**候选回复**\n{reply.full_text()[:2500]}",
        ],
        footer=f"{expire_minutes} 分钟内私聊回复：通过 R{review_id} / 拒绝 R{review_id} / 改 R{review_id} 新内容",
        theme="warning",
    )
    for uid in snap.superusers:
        try:
            await kook.send_card(channel_id=None, user_id=uid, card_json=card_json)
        except Exception as e:
            LOGGER.warning("review notify to {} failed: {}", uid, e)
    await refresh_pending()
    return review_id


async def decide(review_id: int, action: str, actor: str, edited_text: str | None = None) -> ReviewItem:
    if action not in ("approve", "reject", "edit"):
        raise ReviewError(f"未知操作：{action}")
    if action == "edit" and not (edited_text or "").strip():
        raise ReviewError("修改需要附带新内容")
    async with get_session() as session:
        item = await session.get(ReviewItem, review_id, with_for_update=True)
        if item is None:
            raise ReviewError(f"R{review_id} 不存在")
        if item.status != "pending":
            raise ReviewError(f"R{review_id} 已经是 {item.status}")
        if item.expires_at < datetime.now(UTC):
            item.status = "expired"
            raise ReviewError(f"R{review_id} 已过期")
        kook = connected()
        sent_id: str | None = None
        if action != "reject":
            if kook is None:
                raise ReviewError("bot 未连接，无法发送")
            ident = await identity(kook)
            snap = snapshot()
            guild_id = item.guild_id or snap.guild_of(item.channel_id)
            settings = snap.settings(channel_id=item.channel_id, guild_id=guild_id)
            if action == "edit":
                sent_id = await send_kmarkdown(
                    kook,
                    channel_id=item.channel_id,
                    guild_id=guild_id,
                    user_id=item.user_id,
                    reply_to=item.trigger_msg_id,
                    content=edited_text or "",
                    self_id=ident.self_id,
                    nickname=ident.nickname,
                )
            else:
                reply = BotReply.model_validate(item.reply)
                channel_map = (
                    {c.name: c.channel_id for c in snap.guild_channels(guild_id, enabled_only=True) if c.name}
                    if guild_id
                    else {}
                )
                sent_id = await send_reply(
                    kook,
                    channel_id=item.channel_id,
                    guild_id=guild_id,
                    user_id=item.user_id,
                    reply_to=item.trigger_msg_id,
                    reply=reply,
                    meta_text=f"已由管理员审核通过（R{review_id}）",
                    cfg=settings.interaction,
                    self_id=ident.self_id,
                    nickname=ident.nickname,
                    channel_map=channel_map,
                    theme="primary" if settings.interaction.reply_style == "card" else "invisible",
                )
        item.status = {"approve": "approved", "reject": "rejected", "edit": "approved"}[action]
        item.decided_by = actor
        item.decided_at = datetime.now(UTC)
        if item.invocation_id is not None:
            await session.execute(
                update(Invocation)
                .where(Invocation.id == item.invocation_id)
                .values(status="ok" if action != "reject" else "rejected", reply_msg_id=sent_id)
            )
    await refresh_pending()
    return item


async def refresh_pending() -> int:
    global pending_count
    async with get_session() as session:
        pending_count = int(
            await session.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "pending"))
            or 0
        )
    return pending_count


async def expire_loop() -> None:
    while True:
        try:
            async with get_session() as session:
                await session.execute(
                    update(ReviewItem)
                    .where(ReviewItem.status == "pending", ReviewItem.expires_at < func.now())
                    .values(status="expired")
                )
            await refresh_pending()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("review expiry pass failed")
        await asyncio.sleep(60)
