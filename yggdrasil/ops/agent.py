"""The ops agent: a superuser's natural-language admin instruction, carried out with ops tools."""

import secrets
import time
from datetime import datetime
from typing import Literal

from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.usage import RunUsage, UsageLimits

from yggdrasil.agent.schema import OpsReply
from yggdrasil.bot.state import identity
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import INVOCATION_DURATION, INVOCATIONS
from yggdrasil.core.telemetry import TRACER, current_trace_id
from yggdrasil.db.models import Invocation
from yggdrasil.db.session import get_session
from yggdrasil.delivery.send import react, send_kmarkdown
from yggdrasil.ingest.normalize import normalize
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import Incoming, Message
from yggdrasil.llm.errors import describe
from yggdrasil.llm.registry import TaskNotConfigured, chat_model, record_usage
from yggdrasil.llm.streaming import drain_events
from yggdrasil.ops.tools import OpsDeps, QuotedMessage, ops_tools
from yggdrasil.people import People, resolve
from yggdrasil.prompts import render
from yggdrasil.runtime import snapshot
from yggdrasil.settings import InteractionSettings
from yggdrasil.transcript import store as transcripts
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.transcript.schema import ChatContextDisplay, ChatMessage

Outcome = Literal["done", "reply", "failed"]


async def _quoted(incoming: Incoming, self_id: str, bot_name: str) -> QuotedMessage | None:
    r = incoming.reply
    if r is None:
        return None
    try:
        people = await resolve(incoming.guild_id, normalize(r.segments).mentions, bot_name=bot_name, self_id=self_id)
    except Exception:
        LOGGER.exception("name lookup failed")
        people = People(bot_name, self_ids=frozenset({self_id}))
    return QuotedMessage(r.message_id, r.sender.user_id == self_id, normalize(r.segments, people.mention).text)


def _display(incoming: Incoming) -> ChatContextDisplay:
    def snapshot_of(m: Message) -> ChatMessage:
        return ChatMessage(
            kook_message_id=m.message_id,
            channel_id=m.channel_id,
            user_id=m.sender.user_id,
            sender_name=m.sender.name.strip(),
            sent_at=datetime.fromtimestamp(m.time).astimezone(),
            segments=m.segments,
        )

    quoted = snapshot_of(incoming.reply) if incoming.reply is not None else None
    blocks = ["被引用的消息", "指令"] if quoted else ["指令"]
    return ChatContextDisplay(trigger=snapshot_of(incoming), quoted=quoted, blocks=blocks)


def _prompt(instruction: str, quoted: QuotedMessage | None, nonce: str) -> str:
    def block(tag: str, body: str) -> str:
        return f"<{tag}-{nonce}>\n{body.replace(nonce, '*' * len(nonce))}\n</{tag}-{nonce}>"

    parts = []
    if quoted is not None:
        parts.append(block("被引用的消息", ("（你自己发的）" if quoted.is_self else "") + quoted.text))
    parts.append(block("指令", instruction.strip()))
    return "\n\n".join(parts)


async def run_ops(kook: Kook, incoming: Incoming, instruction: str) -> Outcome:
    """Carry out one superuser instruction; anyone else is ignored."""
    snap = snapshot()
    channel_id = incoming.channel_id
    guild_id = incoming.guild_id or (snap.guild_of(channel_id) if channel_id else None)
    if incoming.user_id not in snap.superusers:
        return "failed"
    settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    try:
        resolved = chat_model("ops", default_limits=UsageLimits(request_limit=10, tool_calls_limit=12))
    except TaskNotConfigured:
        await _send(kook, incoming, "ops 任务还没有配置模型，先在控制台的模型路由里配置。")
        return "failed"

    started = time.perf_counter()
    ident = await identity(kook)
    deps = OpsDeps(
        kook=kook,
        user_id=incoming.user_id,
        channel_id=channel_id,
        guild_id=guild_id,
        quoted=await _quoted(incoming, ident.self_id, settings.persona.name),
    )
    deps.notes = TraceNotes(full=settings.privacy.capture_llm_content, bot=(ident.self_id, settings.persona.name))
    if deps.notes.full:
        deps.notes.context = _display(incoming)
    recorder = TranscriptRecorder()
    nonce = secrets.token_hex(3)
    ch_name = snap.channel_name(channel_id) if channel_id else ""
    g_name = snap.guild_name(guild_id) if guild_id else ""
    where = f"服务器「{g_name}」频道 #{ch_name}（{channel_id}）" if channel_id is not None else "私聊"
    outcome: Outcome = "failed"
    reply: OpsReply | None = None
    error: str | None = None
    usage = RunUsage()
    invocation_id = None
    flusher = transcripts.LiveTranscriptFlusher(lambda: invocation_id, recorder, deps.notes, channel_id, guild_id)
    recorder.on_update = flusher.trigger
    with TRACER.start_as_current_span("ygg.invocation ops", kind=trace.SpanKind.SERVER) as span:
        trace_id = current_trace_id()
        async with get_session() as s:
            row = Invocation(
                channel_id=channel_id,
                guild_id=guild_id,
                user_id=incoming.user_id,
                handler="ops",
                trigger_msg_id=incoming.message_id,
                request_text=instruction[:2000],
                status="running",
                models=[resolved.label],
                trace_id=trace_id,
            )
            s.add(row)
            await s.flush()
            invocation_id = row.id
        try:
            await react(kook, channel_id, incoming.message_id, settings.interaction.received_emoji)
            agent: Agent[OpsDeps, OpsReply] = Agent(
                resolved.model,
                output_type=OpsReply,
                deps_type=OpsDeps,
                instructions=render(
                    "ops_system",
                    now=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %A"),
                    where=where,
                    in_channel=channel_id is not None,
                    nonce=nonce,
                ),
                tools=ops_tools(deps),
                name="ops",
                capabilities=[recorder],
                retries=2,
            )
            result = await agent.run(
                _prompt(instruction, deps.quoted, nonce),
                deps=deps,
                model_settings=resolved.settings,
                usage_limits=resolved.limits,
                usage=usage,
                event_stream_handler=drain_events,
            )
            reply = result.output
            outcome = await _deliver(kook, incoming, reply, deps, settings.interaction)
        except Exception as e:
            error = describe(e)[:1000]
            LOGGER.exception("ops run failed")
            span.record_exception(e)
            if channel_id is not None:
                await react(kook, channel_id, incoming.message_id, settings.interaction.failure_emoji)
            else:
                await _send(kook, incoming, f"出错了：{error[:300]}")
        finally:
            await flusher.close()
        span.set_attribute("ygg.status", outcome)
    record_usage(
        "ops",
        resolved.label,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached=usage.cache_read_tokens,
    )
    elapsed = time.perf_counter() - started
    INVOCATIONS.add(1, {"handler": "ops", "outcome": outcome})
    INVOCATION_DURATION.record(elapsed, {"handler": "ops"})
    async with get_session() as s:
        row = await s.get(Invocation, invocation_id)
        if row is not None:
            row.status = outcome
            row.models = [resolved.label]
            row.tool_calls = dict(deps.tool_counts)
            row.tokens_in = usage.input_tokens
            row.tokens_out = usage.output_tokens
            row.tokens_cached = usage.cache_read_tokens
            row.duration_ms = int(elapsed * 1000)
            row.reply = reply.model_dump() if reply else None
            row.error = error
    if recorder.history or recorder.tools:
        await transcripts.save_invocation(invocation_id, recorder, deps.notes, channel_id, guild_id)
    return outcome


async def _deliver(kook: Kook, incoming: Incoming, reply: OpsReply, deps: OpsDeps, cfg: InteractionSettings) -> Outcome:
    in_channel = incoming.channel_id is not None
    text = (reply.text or "").strip()
    if not in_channel and not text:
        text = "；".join(deps.actions) if deps.actions else "没有需要做的操作。"
    if text and (reply.mode == "reply" or not in_channel):
        await _send(kook, incoming, text)
        return "reply"
    await react(kook, incoming.channel_id, incoming.message_id, cfg.action_emoji if deps.actions else cfg.silent_emoji)
    return "done"


async def _send(kook: Kook, incoming: Incoming, text: str) -> None:
    ident = await identity(kook)
    await send_kmarkdown(
        kook,
        channel_id=incoming.channel_id,
        guild_id=incoming.guild_id,
        user_id=incoming.user_id,
        reply_to=incoming.message_id,
        content=text.strip()[:2500],
        self_id=ident.self_id,
        nickname=ident.nickname,
        chat_code=incoming.chat_code,
    )
