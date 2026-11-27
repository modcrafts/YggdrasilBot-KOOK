"""Pure-Python KOOK event router (replaces NoneBot2 plugin + Alconna).

Every KOOK Gateway event passes through :func:`handle_event`:
- channel message deletion -> recall active run & mark DB row recalled
- guild/channel system notices -> :mod:`yggdrasil.events`
- channel / direct messages -> ingest enabled channels, match DB-driven triggers, dispatch to handlers.
"""

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from yggdrasil.access import can_use_bot
from yggdrasil.backfill import request_backfill
from yggdrasil.bot import state as bot_state
from yggdrasil.bot.inflight import spawn as _spawn
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TRIGGERS_MATCHED
from yggdrasil.ingest.writer import WRITER, IncomingMessage
from yggdrasil.kook.client import Kook, LiveKook
from yggdrasil.kook.model import Incoming, KookError
from yggdrasil.kook.parser import parse_incoming, parse_observed, parse_recalled, parse_updated_message
from yggdrasil.pipeline import ratelimit, recalls
from yggdrasil.runtime import Snapshot, snapshot
from yggdrasil.triggers.bots import check_bot_trigger, is_bot_sender, is_ignored_user, note_human_message
from yggdrasil.triggers.index import MatchInput, Resolved, current

_DISCOVER_COOLDOWN_S = 60.0
_discover_tried: dict[str, float] = {}
_discover_inflight: dict[str, asyncio.Task[Snapshot]] = {}


async def _discover_channel(kook: Kook, channel_id: str, guild_id: str) -> Snapshot:
    """Lazily sync an unseen channel in a known enabled guild (coalescing concurrent messages)."""
    snap = snapshot()
    if channel_id in snap.channels:
        return snap
    if time.monotonic() - _discover_tried.get(channel_id, -1e9) < _DISCOVER_COOLDOWN_S:
        return snap
    task = _discover_inflight.get(channel_id)
    if task is None:
        from yggdrasil import events

        async def _run() -> Snapshot:
            try:
                return await events.sync_channel(kook, channel_id, guild_id=guild_id)
            except KookError as e:
                LOGGER.warning("auto-discovering KOOK channel {} failed: {}", channel_id, e)
                return snapshot()
            except Exception:
                LOGGER.exception("auto-discovering KOOK channel {} failed", channel_id)
                return snapshot()
            finally:
                _discover_tried[channel_id] = time.monotonic()
                _discover_inflight.pop(channel_id, None)

        task = asyncio.create_task(_run(), name=f"discover-channel-{channel_id}")
        _discover_inflight[channel_id] = task
    return await task


def _mentions_me(incoming: Incoming) -> bool:
    return incoming.to_me or any(
        seg["type"] == "mention" and seg["data"].get("user_id") == incoming.self_id for seg in incoming.segments
    )


async def handle_event(kook: LiveKook, raw: dict[str, Any]) -> None:
    """Entry point registered on :class:`yggdrasil.kook.gateway.KookGateway`."""
    bot_state.heartbeat(kook.self_id)
    if (recalled := parse_recalled(raw)) is not None:
        _recall(*recalled)
        return
    if (edited := parse_updated_message(raw)) is not None:
        ch_id, msg_id, content, _updated_at = edited
        if snapshot().channel_enabled(ch_id):
            gid = snapshot().guild_of(ch_id) or ""
            _spawn(WRITER.update_message_content(ch_id, msg_id, content, guild_id=gid))
        return
    if (observed := parse_observed(raw)) is not None:
        from yggdrasil import events

        _spawn(events.handle(kook, observed))
        return

    raw_extra = raw.get("extra")
    extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
    guild_id = str(extra.get("guild_id") or "")
    owner_id: str | None = None
    admin_roles: set[int] | None = None
    channel_names: dict[str, str] | None = None
    if guild_id:
        owner_id, admin_roles = await kook.guild_role_info(guild_id)
        snap = snapshot()
        channel_names = {c.channel_id: c.name for c in snap.channels.values() if c.guild_id == guild_id and c.name}

    incoming = parse_incoming(
        raw,
        self_id=kook.self_id,
        owner_id=owner_id,
        admin_role_ids=admin_roles,
        channel_names=channel_names,
    )
    if incoming is not None:
        await dispatch(kook, incoming)


async def dispatch(kook: Kook, incoming: Incoming) -> None:
    """Store an enabled channel's message and route matching triggers to their handler."""
    snap = snapshot()
    channel_id = incoming.channel_id
    guild_id = incoming.guild_id or (snap.guild_of(channel_id) if channel_id else None)
    if incoming.user_id == incoming.self_id:
        return

    if channel_id is not None:
        if channel_id not in snap.channels and guild_id and (g := snap.guilds.get(guild_id)) is not None and g.enabled:
            snap = await _discover_channel(kook, channel_id, guild_id)
        if not snap.channel_enabled(channel_id):
            return
        if guild_id:
            incoming.guild_id = guild_id
        WRITER.submit(
            IncomingMessage(
                channel_id=channel_id,
                guild_id=guild_id or "",
                user_id=incoming.user_id,
                kook_message_id=incoming.message_id,
                sent_at=datetime.fromtimestamp(incoming.time, UTC),
                sender_name=incoming.sender.name.strip(),
                segments=incoming.segments,
            )
        )
        bot_state.last_channel_message[channel_id] = incoming.message_id
        # Any non-bot human speaking in the channel resets the bot-to-bot consecutive-turn breaker.
        if not is_bot_sender(incoming):
            await note_human_message(channel_id)
        scope, role = "channel", incoming.sender.role
    else:
        scope, role = "private", "member"

    if incoming.user_id in snap.superusers:
        role = "superuser"

    settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    reply = incoming.reply
    resolved = current().match(
        MatchInput(
            scope=scope,
            channel_id=channel_id,
            category_id=snap.category_of(channel_id) if channel_id else None,
            guild_id=guild_id,
            text=incoming.text,
            to_me=_mentions_me(incoming),
            replied_to_bot=reply is not None and reply.sender.user_id == incoming.self_id,
            role=role,
        )
    )
    if resolved is None:
        return

    # Per-channel/category/guild feature switches (superusers can still run admin.* commands).
    if channel_id is not None and not _feature_enabled(resolved.handler, settings):
        LOGGER.debug("handler {} disabled in channel {}", resolved.handler, channel_id)
        return

    if role != "superuser":
        if is_ignored_user(incoming, settings):
            LOGGER.info(
                "ignored {} trigger from ignored user {} in channel {}",
                resolved.handler,
                incoming.user_id,
                channel_id,
            )
            return
        allowed, deny_reason = can_use_bot(incoming, settings, role=role)
        if not allowed:
            LOGGER.info(
                "blocked {} trigger from user {} in channel {} ({})",
                resolved.handler,
                incoming.user_id,
                channel_id,
                deny_reason,
            )
            return
        if is_bot_sender(incoming):
            if channel_id is None:
                LOGGER.info("ignored private {} trigger from bot {}", resolved.handler, incoming.user_id)
                return
            if reason := await check_bot_trigger(channel_id, incoming.user_id, resolved.handler, settings.interaction):
                LOGGER.info(
                    "blocked {} trigger from bot {} in channel {} ({})",
                    resolved.handler,
                    incoming.user_id,
                    channel_id,
                    reason,
                )
                return
    if not await ratelimit.cooldown(resolved.rule_id, incoming.user_id, resolved.cooldown_s):
        return

    # Lazily enrich quoted message if KOOK Gateway's ``extra.quote`` omitted attachment/card details.
    if incoming.reply is not None and channel_id is not None and _quote_needs_enrichment(incoming.reply):
        try:
            full = await kook.message(channel_id=channel_id, msg_id=incoming.reply.message_id)
            if full is not None and full.segments:
                incoming.reply = full
        except Exception as e:
            LOGGER.debug("enriching quoted message {} failed: {}", incoming.reply.message_id, e)

    TRIGGERS_MATCHED.add(1, {"handler": resolved.handler, "scope": scope})
    await _invoke_handler(kook, incoming, resolved)


def _quote_needs_enrichment(reply: Any) -> bool:
    """Whether KOOK Gateway's compact ``extra.quote`` likely missed attachment/filename/card details."""
    if not reply.segments:
        return True
    for s in reply.segments:
        st = s.get("type")
        data = s.get("data") or {}
        if st == "file" and (not data.get("file_name") or data.get("file_name") == "file"):
            return True
        if st in ("image", "video", "audio") and not data.get("url"):
            return True
    return False


def _feature_enabled(handler: str, settings: Any) -> bool:
    f = settings.features
    if handler == "chat":
        return bool(f.chat)
    if handler == "tldr":
        return bool(f.tldr)
    if handler.startswith("privacy."):
        return bool(f.privacy_commands)
    return True


def _recall(channel_id: str, message_id: str) -> None:
    recalls.recall(channel_id, message_id)
    if snapshot().channel_enabled(channel_id):
        _spawn(WRITER.mark_recalled(channel_id, message_id))


# --- handlers ---------------------------------------------------------------------------------------------------


async def _invoke_handler(kook: Kook, incoming: Incoming, resolved: Resolved) -> None:
    handler = resolved.handler
    args = resolved.args.strip()
    if handler in ("tldr", "chat"):
        from yggdrasil.pipeline.invoke import run

        _spawn(run(kook, incoming, resolved, handler, args))
    elif handler == "privacy.optout":
        await _handle_privacy(kook, incoming, "optout")
    elif handler == "privacy.optin":
        await _handle_privacy(kook, incoming, "optin")
    elif handler == "privacy.forget":
        await _handle_privacy(kook, incoming, "forget")
    elif handler == "admin.review":
        await _handle_review(kook, incoming, args)
    elif handler == "admin.status":
        await _handle_status(kook, incoming)
    elif handler == "admin.ops":
        if incoming.user_id in snapshot().superusers:
            from yggdrasil.ops.agent import run_ops

            _spawn(run_ops(kook, incoming, args))
    else:
        LOGGER.warning("unknown trigger handler: {}", handler)


async def _privacy_reply(kook: Kook, incoming: Incoming, channel_id: str, text: str) -> None:
    from yggdrasil.delivery.send import send_kmarkdown

    ident = await bot_state.identity(kook)
    settings = snapshot().settings(channel_id=channel_id, guild_id=incoming.guild_id)
    quote = (
        incoming.message_id
        if bot_state.should_quote(channel_id, incoming.message_id, settings.interaction.quote_reply)
        else None
    )
    await send_kmarkdown(
        kook,
        channel_id=channel_id,
        guild_id=incoming.guild_id,
        user_id=incoming.user_id,
        reply_to=quote,
        content=text,
        self_id=ident.self_id,
        nickname=ident.nickname,
    )


async def _handle_privacy(kook: Kook, incoming: Incoming, action: str) -> None:
    from yggdrasil.memory.privacy import forget_user, set_opt_out

    channel_id = incoming.channel_id
    guild_id = incoming.guild_id or (snapshot().guild_of(channel_id) if channel_id else None)
    if channel_id is None or not guild_id:
        return
    if action == "optout":
        await set_opt_out(guild_id, incoming.user_id, True)
        await _privacy_reply(
            kook,
            incoming,
            channel_id,
            "好，之后在这个服务器里不再整理你的画像（聊天记录仍按频道规则保存）。想恢复就说「记得我」。",
        )
    elif action == "optin":
        await set_opt_out(guild_id, incoming.user_id, False)
        await _privacy_reply(kook, incoming, channel_id, "好，重新开始在这个服务器里记住你。")
    elif action == "forget":
        await forget_user(guild_id, incoming.user_id)
        await set_opt_out(guild_id, incoming.user_id, True)
        await _privacy_reply(kook, incoming, channel_id, "已删除你在本服务器的画像，并且之后不再整理。")


def _parse_review_args(args: str) -> tuple[str | None, str, str]:
    """Parse ``approve <id> | reject <id> | edit <id> <text...>``."""
    parts = args.split(None, 2)
    if not parts:
        return None, "", ""
    action = parts[0].lower()
    if action not in ("approve", "reject", "edit"):
        return None, "", ""
    raw_id = parts[1] if len(parts) >= 2 else ""
    text = parts[2] if len(parts) >= 3 else ""
    return action, raw_id, text


async def _handle_review(kook: Kook, incoming: Incoming, args: str) -> None:
    from yggdrasil.delivery.send import send_kmarkdown
    from yggdrasil.moderation.review import ReviewError, decide, parse_review_id

    if incoming.channel_id is not None:
        return
    action, raw_id, edited_text = _parse_review_args(args)
    try:
        review_id = parse_review_id(raw_id) if raw_id else None
    except ReviewError:
        review_id = None
    if action is None or review_id is None:
        from yggdrasil.ops.agent import run_ops

        _spawn(run_ops(kook, incoming, incoming.text))
        return
    ident = await bot_state.identity(kook)
    try:
        item = await decide(review_id, action, f"kook:{incoming.user_id}", edited_text or None)
        msg = f"R{item.id} → {item.status}"
    except ReviewError as e:
        msg = str(e)
    await send_kmarkdown(
        kook,
        channel_id=None,
        guild_id=None,
        user_id=incoming.user_id,
        reply_to=incoming.message_id,
        content=msg,
        self_id=ident.self_id,
        nickname=ident.nickname,
        chat_code=incoming.chat_code,
    )


async def _handle_status(kook: Kook, incoming: Incoming) -> None:
    from yggdrasil.delivery.send import send_kmarkdown
    from yggdrasil.moderation.review import pending_count

    snap = snapshot()
    enabled = [c for c in snap.channels.values() if not c.is_category and snap.channel_enabled(c.channel_id)]
    ident = await bot_state.identity(kook)
    ch_text = ", ".join(f"#{c.name}({c.channel_id})" for c in enabled) or "无"
    msg = (
        f"config v{snap.version}\n"
        f"服务器：{len(snap.guilds)} 个  启用频道：{ch_text}\n"
        f"路由：{', '.join(sorted(snap.routes)) or '无'}\n"
        f"待审核：{pending_count}"
    )
    await send_kmarkdown(
        kook,
        channel_id=incoming.channel_id,
        guild_id=incoming.guild_id,
        user_id=incoming.user_id,
        reply_to=incoming.message_id,
        content=msg,
        self_id=ident.self_id,
        nickname=ident.nickname,
        chat_code=incoming.chat_code,
    )


# --- lifecycle --------------------------------------------------------------------------------------------------


async def on_connected(kook: LiveKook) -> None:
    bot_state.mark_connected(kook.self_id)
    ident = await bot_state.identity(kook)
    LOGGER.info("KOOK bot connected: {} ({})", ident.nickname, ident.self_id)
    _spawn(_catch_up(kook, datetime.now(UTC)))


async def _catch_up(kook: LiveKook, connected_at: datetime) -> None:
    """Sync new/updated channels and backfill messages missed while no pod held the KOOK gateway connection."""
    await asyncio.sleep(10)
    try:
        from yggdrasil import events

        await events.sync_registered_guilds(kook)
    except Exception:
        LOGGER.exception("auto-syncing KOOK channels on connect failed")
    try:
        from yggdrasil.stickers.collect import repair_emoji_stickers

        await repair_emoji_stickers(kook)
    except Exception:
        LOGGER.exception("repairing KOOK emoji sticker guild_ids on connect failed")
    snap = snapshot()
    for cid, ch in snap.channels.items():
        if not ch.is_category and not ch.is_forum and snap.channel_enabled(cid):
            request_backfill(cid, before=connected_at)


async def on_disconnected(kook: LiveKook) -> None:
    bot_state.mark_disconnected(kook.self_id)
    LOGGER.warning("KOOK bot disconnected: {}", kook.self_id)
