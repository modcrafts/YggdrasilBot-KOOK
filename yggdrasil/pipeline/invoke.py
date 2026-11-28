"""One triggered run (chat / tldr): moderation, status reactions, agent execution, adaptive delivery, bookkeeping."""

import asyncio
import contextlib
import time
from datetime import timedelta
from typing import Literal
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.usage import RunUsage, UsageLimits

from yggdrasil.access import effective_tools
from yggdrasil.agent import bridge
from yggdrasil.agent.budget import ToolBudget
from yggdrasil.agent.context import build_context, build_followup_context
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.media import keep_context_images
from yggdrasil.agent.permissions import Role, may_recall, speaker_role
from yggdrasil.agent.schema import BotReply
from yggdrasil.agent.skills import skill_names, skills_for
from yggdrasil.agent.tools import ALL_TOOLS, action_tools, suite_enabled, tool_names, tools_for
from yggdrasil.agent.tools import sandbox as sandbox_tools
from yggdrasil.agent.tools.images import check_reply_images, reply_images
from yggdrasil.bot.state import identity, should_quote
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import INVOCATION_ACK_LATENCY, INVOCATION_DURATION, INVOCATIONS
from yggdrasil.core.telemetry import TRACER, current_trace_id
from yggdrasil.delivery.send import (
    react,
    resolve_message_anchors,
    resolve_thread_anchors,
    send_kmarkdown,
    send_reply,
    unreact,
)
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import Incoming
from yggdrasil.llm.errors import describe, refusal
from yggdrasil.llm.registry import ResolvedModel, TaskNotConfigured, chat_model, record_usage
from yggdrasil.llm.streaming import RUN_STATS, RunStats, drain_events
from yggdrasil.moderation.guard import Verdict, moderate
from yggdrasil.pipeline import ack as ack_mod
from yggdrasil.pipeline import ratelimit, recalls, record, repeats
from yggdrasil.pipeline import session as sessions
from yggdrasil.pipeline.prompt import inline_images, instructions, now_text
from yggdrasil.runtime import snapshot
from yggdrasil.sandbox import Session, manager
from yggdrasil.settings import Settings
from yggdrasil.transcript import store as transcripts
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes
from yggdrasil.triggers.index import Resolved

Handler = Literal["chat", "tldr"]
_SEEN_SLACK = timedelta(hours=1)


async def run(kook: Kook, incoming: Incoming, resolved: Resolved, handler: Handler, request_text: str) -> None:
    snap = snapshot()
    channel_id = incoming.channel_id
    guild_id = incoming.guild_id or (snap.guild_of(channel_id) if channel_id else None)
    settings = snap.settings(channel_id=channel_id, guild_id=guild_id)
    role = speaker_role(incoming, snap.superusers)
    superuser = role == "superuser"
    user_id, message_id = incoming.user_id, incoming.message_id
    repeat = None if superuser else repeats.key(incoming, resolved, handler, request_text)
    if repeat is not None and not await repeats.first(repeat, settings.ratelimit.repeat_window_s):
        LOGGER.info("repeated {} request ignored user={} channel={}", handler, user_id, channel_id)
        INVOCATIONS.add(1, {"handler": handler, "outcome": "repeat"})
        if settings.features.status_reactions:
            await react(kook, channel_id, message_id, settings.interaction.silent_emoji)
        return
    answered = False
    try:
        if reason := await ratelimit.check(channel_id, user_id, settings.ratelimit, superuser=superuser):
            LOGGER.info("rate limited ({}) user={} channel={}", reason, user_id, channel_id)
            INVOCATIONS.add(1, {"handler": handler, "outcome": "ratelimited"})
            if settings.features.status_reactions:
                await react(kook, channel_id, message_id, settings.interaction.ratelimited_emoji)
            return
        with recalls.watch(channel_id, message_id) as watch:
            async with ratelimit.slot(channel_id, user_id, settings.ratelimit) as granted:
                if not granted:
                    LOGGER.info("request dropped: queue full or timed out user={} channel={}", user_id, channel_id)
                    INVOCATIONS.add(1, {"handler": handler, "outcome": "busy"})
                    if settings.features.status_reactions:
                        await react(kook, channel_id, message_id, settings.interaction.ratelimited_emoji)
                    return
                if watch.recalled:
                    LOGGER.info("{} request deleted while queued user={} channel={}", handler, user_id, channel_id)
                    INVOCATIONS.add(1, {"handler": handler, "outcome": "cancelled"})
                    return
                inv = Invocator(
                    kook, incoming, resolved, handler, request_text, settings, channel_id, guild_id, role, watch=watch
                )
                status = await inv.run()
                answered = status not in ("failed", "cancelled")
    finally:
        if repeat is not None and not answered:
            with contextlib.suppress(Exception):
                await repeats.forget(repeat)


def _cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _clean_reaction(raw: str | None) -> str | None:
    emoji = (raw or "").strip()
    if not emoji or len(emoji) > 64 or any(ch.isspace() for ch in emoji):
        return None
    return emoji


class Invocator:
    def __init__(
        self,
        kook: Kook,
        incoming: Incoming,
        resolved: Resolved,
        handler: Handler,
        request_text: str,
        settings: Settings,
        channel_id: str | None,
        guild_id: str | None,
        role: Role = "member",
        *,
        watch: recalls.Watch | None = None,
    ) -> None:
        self._setup(
            kook,
            handler,
            settings,
            channel_id,
            guild_id,
            user_id=incoming.user_id,
            trigger_message_id=incoming.message_id,
            role=role,
            chat_code=incoming.chat_code,
        )
        self.incoming, self.resolved, self.request_text = incoming, resolved, request_text
        self.rule_id = resolved.rule_id
        if watch is not None:
            self.watch = watch

    def _setup(
        self,
        kook: Kook,
        handler: Handler,
        settings: Settings,
        channel_id: str | None,
        guild_id: str | None,
        *,
        user_id: str,
        trigger_message_id: str | None,
        role: Role,
        record_handler: str | None = None,
        chat_code: str | None = None,
    ) -> None:
        self.kook, self.handler, self.settings = kook, handler, settings
        self.channel_id, self.guild_id = channel_id, guild_id
        self.user_id, self.trigger_message_id = user_id, trigger_message_id
        self.chat_code = chat_code
        self.record_handler: str = record_handler or handler
        self.rule_id: int | None = None
        self.role: Role = role
        self.reply_msg_id: str | None = None
        self.started = time.perf_counter()
        self.meta = record.Meta()
        self.invocation_id: UUID | None = None
        self.ack_kind: str | None = None
        self._finished = False
        self.deps: Deps | None = None
        self.sandbox: Session | None = None
        self._keep: tuple[sessions.Setup, str] | None = None
        self.recorder = TranscriptRecorder()
        self.notes = TraceNotes(full=settings.privacy.capture_llm_content)
        self._flusher = transcripts.LiveTranscriptFlusher(
            lambda: self.invocation_id, self.recorder, self.notes, self.channel_id, self.guild_id
        )
        self.recorder.on_update = self._flusher.trigger
        self.watch = recalls.Watch()
        self._slow: asyncio.Task[None] | None = None
        self._received_reacted = False

    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    async def run(self) -> str:
        attrs = {
            "ygg.handler": self.record_handler,
            "ygg.channel_id": self.channel_id or "",
            "ygg.guild_id": self.guild_id or "",
            "ygg.rule_id": self.rule_id or 0,
        }
        with TRACER.start_as_current_span(
            f"ygg.invocation {self.record_handler}", kind=trace.SpanKind.SERVER, attributes=attrs
        ) as span:
            self.meta.trace_id = current_trace_id()
            status, error = "failed", None
            try:
                if await self._status(self.settings.interaction.received_emoji):
                    self._received_reacted = True
                    self._mark_ack("react")
                self._slow = asyncio.create_task(self._mark_slow())
                status = await self._run()
            except Exception as e:
                error = describe(e)[:1000]
                LOGGER.exception("invocation {} failed", self.record_handler)
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, error[:200]))
                if not await self._notify_refusal(e):
                    await self._status(self.settings.interaction.failure_emoji)
            finally:
                self._stop_slow()
                span.set_attribute("ygg.status", status)
                if not self._finished:
                    self.meta.total_s = self.elapsed()
                INVOCATIONS.add(1, {"handler": self.record_handler, "outcome": status})
                INVOCATION_DURATION.record(self.meta.total_s, {"handler": self.record_handler})
                if not self._finished:
                    await self._finish(status, error)
                if self.sandbox is not None:
                    with contextlib.suppress(Exception):
                        await self.sandbox.close()
        return status

    async def _start_row(self, request_text: str) -> None:
        self.invocation_id = await record.start_row(
            channel_id=self.channel_id,
            guild_id=self.guild_id,
            user_id=self.user_id,
            handler=self.record_handler,
            trigger_msg_id=self.trigger_message_id,
            request_text=request_text,
            trace_id=self.meta.trace_id,
        )

    async def _save_verdict(self, v: Verdict) -> None:
        if v.verdict == "Skipped" or self.invocation_id is None:
            return
        await record.save_verdict(self.invocation_id, v)

    async def _finish(self, status: str, error: str | None, **extra: object) -> None:
        self._finished = True
        self.meta.total_s = self.elapsed()
        await self._flusher.close()
        try:
            await ratelimit.add_tokens(self.channel_id, self._billable_tokens())
        except Exception:
            LOGGER.exception("recording token usage for invocation {} failed", self.invocation_id)
        if self.invocation_id is None:
            return
        # Keep the row running until the console can read the final transcript. Delivery already happened.
        try:
            if self.recorder.history or self.recorder.tools:
                if not self.watch.recalled:
                    with contextlib.suppress(Exception):
                        await keep_context_images(self.notes, self.channel_id)
                await transcripts.save_invocation(
                    self.invocation_id, self.recorder, self.notes, self.channel_id, self.guild_id
                )
        finally:
            await record.finish_row(self.invocation_id, status, error, self.meta, self.ack_kind, **extra)

    def _billable_tokens(self) -> int:
        return self.meta.tokens_in + self.meta.tokens_out

    async def _run(self) -> str:
        ident = await identity(self.kook)
        persona = self.settings.persona.name
        self.notes.bot = (ident.self_id, persona)
        replied_bot_msg = self._replied_bot_message(ident.self_id)
        recall_target = (
            replied_bot_msg
            if replied_bot_msg is not None
            and self.channel_id is not None
            and await may_recall(self.role, self.user_id, self.channel_id, replied_bot_msg)
            else None
        )
        recall = None if replied_bot_msg is None else recall_target is not None
        session = await self._session(replied_bot_msg)
        if session is not None and replied_bot_msg is not None and self.channel_id is not None:
            route, sandbox_on = session.setup.handler, session.setup.sandbox
            ctx = await build_followup_context(
                self.incoming,
                self.settings,
                self_id=ident.self_id,
                bot_name=persona,
                request_text=self.request_text,
                handles=session.handles,
                nonce=session.nonce,
                answer_msg_id=replied_bot_msg,
                seen_message_id=session.seen_message_id,
                since=session.created_at - (_SEEN_SLACK if session.seen_message_id is not None else timedelta()),
                now=now_text(),
                recall=bool(recall),
                workspace=sandbox_on,
            )
            self.recorder.prior = session.messages
            self.recorder.continued_from = str(session.invocation_id)
        else:
            session = None
            route, sandbox_on = self.handler, self._suite_enabled(self.handler)
            ctx = await build_context(
                self.incoming,
                self.resolved,
                self.settings,
                self_id=ident.self_id,
                bot_name=persona,
                request_text=self.request_text,
                workspace=sandbox_on,
                recall=recall,
            )
        await self._start_row(self.request_text or ctx.trigger.text)
        try:
            resolved_model = chat_model(route)
        except TaskNotConfigured:
            LOGGER.warning("task {} has no model route; configure it in the dashboard", route)
            await self._status(self.settings.interaction.failure_emoji)
            return "failed"

        mod_text = f"{ctx.request_text or ctx.trigger.text}\n{ctx.target.text if ctx.target is not ctx.trigger else ''}"
        role_ids = self._role_ids()
        deps = Deps(
            self.kook,
            self.channel_id,
            self.guild_id,
            self.user_id,
            self.settings,
            ctx.handles,
            route,
            recall_target=recall_target,
            self_id=ident.self_id,
            bot_nickname=ident.nickname,
            chat_code=self.chat_code,
            trigger_message_id=self.trigger_message_id,
            invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
            request_text=ctx.request_text or ctx.trigger.text,
            notes=self.notes,
            role_ids=role_ids,
        )
        if self.notes.full:
            self.notes.context = ctx.display
        tools_cfg = self.settings.tools
        allowed_tools = self._effective_tools()
        tool_list = session.setup.tools if session else tool_names(allowed_tools, self.handler)
        skill_list = session.setup.skills if session else skill_names(self.settings, self.handler, self.channel_id)
        suite = sandbox_tools.Suite()
        if sandbox_on and self.channel_id is not None:
            self.sandbox = deps.sandbox = manager().session(
                self.channel_id,
                invocation_id=str(self.invocation_id) if self.invocation_id is not None else None,
                cfg=tools_cfg,
            )
            suite = sandbox_tools.suite(exclude=frozenset() if tools_cfg.sandbox_delegate else frozenset({"delegate"}))
        system = (
            session.setup.instructions
            if session
            else instructions(
                self.handler,
                self.settings,
                self.channel_id,
                guild_id=self.guild_id,
                tools=tool_list + suite.names,
                sandbox=tools_cfg if sandbox_on else None,
                report=False,
                nonce=ctx.nonce,
                role_ids=role_ids,
            )
        )
        accessible = self._accessible_channels(role_ids)
        self._keep = (
            sessions.Setup(route, system, tool_list, sandbox_on, skill_list, accessible),
            ctx.nonce,
        )
        agent = self._agent(
            resolved_model,
            tools=tools_for(tool_list) + action_tools() + suite.tools,
            capabilities=suite.capabilities + skills_for(skill_list),
            instructions=system,
        )
        prompt = await inline_images(ctx, route, self.settings.context.inline_images, self.notes)
        return await self._execute(
            agent, prompt, deps, resolved_model, mod_text, history=session.messages if session else None
        )

    def _role_ids(self) -> tuple[str, ...]:
        incoming = getattr(self, "incoming", None)
        if incoming is None:
            return ()
        return tuple(str(r) for r in incoming.sender.role_ids)

    def _accessible_channels(self, role_ids: tuple[str, ...] | None = None) -> list[str] | None:
        if not self.guild_id:
            return None
        from yggdrasil.memory.search import accessible_channels

        return accessible_channels(
            self.guild_id, self.channel_id, role_ids=self._role_ids() if role_ids is None else role_ids
        )

    def _effective_tools(self) -> list[str]:
        role = "superuser" if self.user_id in snapshot().superusers else self.incoming.sender.role
        return effective_tools(self.settings, self.incoming, role=role)

    def _suite_enabled(self, handler: str) -> bool:
        return suite_enabled(
            self.settings,
            handler,
            self.channel_id,
            global_settings=snapshot().settings(),
            configured=manager().configured,
            enabled_tools=self._effective_tools(),
        )

    def _agent(
        self,
        resolved_model: ResolvedModel,
        *,
        tools: list[Tool[Deps]],
        capabilities: list[AbstractCapability[Deps]],
        instructions: str,
    ) -> Agent[Deps, BotReply]:
        agent = Agent(
            resolved_model.model,
            output_type=BotReply,
            deps_type=Deps,
            instructions=instructions,
            tools=tools,
            capabilities=[self.recorder, self._budget(), *capabilities],
            name=self.handler,
            retries=2,
        )
        agent.output_validator(check_reply_images)
        return agent

    async def _execute(
        self,
        agent: Agent[Deps, BotReply],
        prompt: str | list[UserContent],
        deps: Deps,
        resolved_model: ResolvedModel,
        mod_text: str,
        *,
        history: list[ModelMessage] | None = None,
    ) -> str:
        if self.watch.recalled:
            return await self._recalled()
        budget = self._budget()
        run_usage = RunUsage()
        run_stats = RunStats()
        stats_token = RUN_STATS.set(run_stats)
        agent_task = asyncio.create_task(
            agent.run(
                prompt,
                message_history=history,
                deps=deps,
                model_settings=resolved_model.settings,
                usage_limits=UsageLimits(request_limit=budget.requests),
                usage=run_usage,
                event_stream_handler=drain_events,
                workspace=self.sandbox.backend if self.sandbox is not None else None,
            )
        )
        RUN_STATS.reset(stats_token)
        input_task = asyncio.create_task(
            moderate("input", mod_text, channel_id=self.channel_id, guild_id=self.guild_id)
        )
        self.watch.guard(agent_task)
        self.watch.guard(input_task)
        blocked: Verdict | None = None
        try:
            v_in = await input_task
            await self._save_verdict(v_in)
            if v_in.action != "pass" and self.settings.moderation.input_action_on_hit == "drop":
                blocked = v_in
            else:
                result = await agent_task
        except asyncio.CancelledError:
            if not self.watch.recalled or _cancelling():
                raise
        finally:
            for t in (agent_task, input_task):
                if not t.done():
                    t.cancel()
            self._stop_slow()
            self._account(resolved_model.label, run_usage, deps, run_stats)
            await bridge.close_run(deps)
        if self.watch.recalled:
            return await self._recalled()

        if blocked is not None:
            await self._status(self.settings.interaction.review_emoji)
            if self.settings.moderation.notify_admins_on_input_block:
                from yggdrasil.moderation.review import notify_admins

                cats = ",".join(blocked.categories)
                await notify_admins(
                    self.kook, f"输入被拦截（{blocked.verdict} {blocked.score:.2f} {cats}）\n{mod_text[:500]}"
                )
            await self._finish("blocked_input", None, input_verdict=blocked.as_dict())
            return "blocked_input"

        reply = result.output
        self.deps = deps

        if reply.mode == "done" and deps.actions:
            await self._status(self.settings.interaction.action_emoji)
            await self._finish("done", None, reply=reply.model_dump())
            return "done"
        if reply.mode != "reply":
            return await self._stay_silent(reply)

        v_out = await moderate(
            "output", mod_text, reply.full_text(), channel_id=self.channel_id, guild_id=self.guild_id
        )
        await self._save_verdict(v_out)
        checked = [f"输入 {v_in.verdict}"] if v_in.verdict != "Skipped" else []
        if v_out.verdict != "Skipped":
            checked.append(f"输出 {v_out.verdict}({v_out.score:.2f})")
        self.meta.moderation = " · ".join(checked)
        verdicts = {"input_verdict": v_in.as_dict(), "output_verdict": v_out.as_dict(), "reply": reply.model_dump()}
        if self.watch.recalled:
            return await self._recalled(**verdicts)

        if v_out.action == "pass":
            await self._keep_session(result.all_messages(), deps.handles)
            await self.deliver(reply)
            await self._finish("ok", None, reply_msg_id=self.reply_msg_id, **verdicts)
            return "ok"
        if v_out.action == "review" and self.channel_id is not None:
            from yggdrasil.moderation.review import create_review

            await create_review(
                self.kook,
                invocation_id=self.invocation_id,
                channel_id=self.channel_id,
                guild_id=self.guild_id,
                user_id=self.user_id,
                trigger_msg_id=self.trigger_message_id,
                request_text=mod_text,
                reply=reply,
                verdict=v_out,
                expire_minutes=self.settings.moderation.review_expire_minutes,
            )
            if self.settings.moderation.notify_channel_on_review:
                with contextlib.suppress(Exception):
                    notice = await ack_mod.review_notice(mod_text, self.settings)
                    if notice:
                        await self._send_text(notice)
            await self._status(self.settings.interaction.review_emoji)
            await self._finish("review", None, **verdicts)
            return "review"
        await self._status(self.settings.interaction.review_emoji)
        await self._finish("rejected", None, **verdicts)
        return "rejected"

    async def _send_text(self, text: str, *, allow_quote: bool = True) -> None:
        ident = await identity(self.kook)
        quote = (
            self.trigger_message_id
            if allow_quote
            and self.trigger_message_id
            and should_quote(self.channel_id, self.trigger_message_id, self.settings.interaction.quote_reply)
            else None
        )
        await send_kmarkdown(
            self.kook,
            channel_id=self.channel_id,
            guild_id=self.guild_id,
            user_id=self.user_id,
            reply_to=quote,
            content=text,
            self_id=ident.self_id,
            nickname=ident.nickname,
            chat_code=self.chat_code,
        )

    def _mark_ack(self, kind: str) -> None:
        self.ack_kind = kind
        if self.meta.ack_s is None:
            self.meta.ack_s = self.elapsed()
            INVOCATION_ACK_LATENCY.record(self.meta.ack_s, {"handler": self.record_handler, "kind": kind})

    async def _status(self, emoji: str) -> bool:
        if not self.settings.features.status_reactions or not emoji or not self.trigger_message_id:
            return False
        return (await react(self.kook, self.channel_id, self.trigger_message_id, emoji)) is not False

    async def _unstatus(self, emoji: str) -> bool:
        if not self.settings.features.status_reactions or not emoji or not self.trigger_message_id:
            return False
        return (await unreact(self.kook, self.channel_id, self.trigger_message_id, emoji)) is not False

    async def _notify_refusal(self, exc: Exception) -> bool:
        """The provider's content filter cut the answer off: say so in the chat with the provider's reason, where a
        bare failure reaction reads as the bot breaking. False when there is nothing to say or it didn't go out."""
        told = refusal(exc)
        if told is None or not self.settings.interaction.refusal_notice:
            return False
        text = "模型拒绝了这个请求" + (f"（{told.category}）" if told.category else "")
        if told.explanation:
            text += f"：{told.explanation[:500]}"
        try:
            await self._send_text(text)
        except Exception:
            LOGGER.exception("refusal notice failed")
            return False
        return True

    async def _mark_slow(self) -> None:
        await asyncio.sleep(self.settings.interaction.slow_after_s)
        with contextlib.suppress(Exception):
            await self._status(self.settings.interaction.slow_emoji)

    def _stop_slow(self) -> None:
        if self._slow is not None:
            self._slow.cancel()
            self._slow = None

    def _budget(self) -> ToolBudget:
        cfg = self.settings.tools
        if self.sandbox is not None:
            return ToolBudget(requests=cfg.sandbox_request_limit, tool_calls=cfg.sandbox_tool_calls_limit)
        return ToolBudget(requests=cfg.request_limit, tool_calls=cfg.tool_calls_limit)

    def _account(self, model_label: str, usage: RunUsage, deps: Deps, stats: RunStats) -> None:
        self.meta.models.append(model_label)
        self.meta.tokens_in += usage.input_tokens
        self.meta.tokens_out += usage.output_tokens
        self.meta.tokens_cached += usage.cache_read_tokens
        for label, tin, tout in deps.vision_tokens:
            self.meta.models.append(label)
            self.meta.tokens_in += tin
            self.meta.tokens_out += tout
        self.meta.tools = dict(deps.tool_counts)
        self.meta.tool_seconds = dict(deps.tool_seconds)
        self.meta.slow_tools = frozenset(self.settings.tools.slow_tools)
        self.meta.model_names = stats.models
        self.meta.ttft_s = stats.ttft
        self.meta.tokens_per_s = stats.tokens_per_s
        record_usage(
            self.handler,
            model_label,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached=usage.cache_read_tokens,
        )

    async def _stay_silent(self, reply: BotReply) -> str:
        reason = (reply.silent_reason or "").strip()[:200]
        LOGGER.info("{} stays silent: {}", self.handler, reason or "(no reason)")
        trace.get_current_span().set_attribute("ygg.silent_reason", reason)
        cfg = self.settings.interaction
        mode = cfg.silent_reaction_mode
        custom = _clean_reaction(reply.silent_reaction) if mode == "auto" else None
        fallback = "" if mode == "none" else cfg.silent_emoji
        target = custom or fallback
        if self._received_reacted and cfg.received_emoji and target != cfg.received_emoji:
            await self._unstatus(cfg.received_emoji)
        if custom:
            if (custom != cfg.received_emoji or not self._received_reacted) and not await self._status(custom):
                if fallback:
                    await self._status(fallback)
        elif fallback and (fallback != cfg.received_emoji or not self._received_reacted):
            await self._status(fallback)
        await self._finish("silent", None, reply=reply.model_dump())
        return "silent"

    async def _recalled(self, **extra: object) -> str:
        LOGGER.info("{} trigger {} deleted; run stopped", self.handler, self.trigger_message_id)
        await self._finish("cancelled", "原消息已删除", **extra)
        return "cancelled"

    def _replied_bot_message(self, self_id: str) -> str | None:
        r = self.incoming.reply
        if self.handler != "chat" or self.channel_id is None or r is None or r.sender.user_id != self_id:
            return None
        return r.message_id

    async def _session(self, answer: str | None) -> sessions.Session | None:
        minutes = self.settings.context.continue_minutes
        if answer is None or self.channel_id is None or minutes <= 0 or not self.settings.privacy.capture_llm_content:
            return None
        try:
            found = await sessions.load(self.channel_id, answer, timedelta(minutes=minutes))
        except Exception:
            LOGGER.exception("session lookup failed")
            return None
        if found is None:
            return None
        setup = found.setup
        allowed_tools = set(tool_names(self._effective_tools(), setup.handler))
        current_accessible = set(self._accessible_channels() or ())
        if (
            any(name not in ALL_TOOLS for name in setup.tools)
            or not set(setup.tools) <= allowed_tools
            or (setup.sandbox and not self._suite_enabled(setup.handler))
            or not set(setup.skills) <= set(skill_names(self.settings, self.handler, self.channel_id))
            or (setup.accessible_channels is not None and not set(setup.accessible_channels) <= current_accessible)
        ):
            LOGGER.info("invocation {} used tools or channels this run can't have; starting over", found.invocation_id)
            return None
        LOGGER.info("continuing the conversation of invocation {}", found.invocation_id)
        return found

    async def _keep_session(self, messages: list[ModelMessage], handles: Handles) -> None:
        if (
            self._keep is None
            or self.invocation_id is None
            or self.channel_id is None
            or not self.trigger_message_id
            or not self.settings.privacy.capture_llm_content
        ):
            return
        from yggdrasil.memory.search import find_by_msg_id

        try:
            seen = await find_by_msg_id(self.channel_id, self.trigger_message_id)
        except Exception:
            LOGGER.exception("trigger lookup failed")
            seen = None
        setup, nonce = self._keep
        await sessions.save(self.invocation_id, setup, messages, handles, nonce, seen.id if seen else None)

    def _channel_map(self) -> dict[str, str]:
        """``channel_name -> channel_id`` map in this guild for ``#频道名`` -> ``(chn)id(chn)``."""
        if not self.guild_id:
            return {}
        return {
            c.name.strip(): c.channel_id
            for c in snapshot().guild_channels(self.guild_id, enabled_only=False, include_forums=True)
            if c.name and c.name.strip()
        }

    async def deliver(self, reply: BotReply) -> None:
        cfg = self.settings.interaction
        ident = await identity(self.kook)
        deps = self.deps
        images = await reply_images(deps, reply) if deps is not None else {}
        self.meta.total_s = self.elapsed()
        quote = (
            self.trigger_message_id
            if self.trigger_message_id and should_quote(self.channel_id, self.trigger_message_id, cfg.quote_reply)
            else None
        )
        meta_text = self.meta.render(list(cfg.metadata_fields)) if cfg.metadata_node else None
        is_structured_report = self.record_handler in ("tldr", "report")
        card_theme = "primary" if (is_structured_report or cfg.reply_style == "card") else "invisible"
        role_ids = deps.role_ids if deps is not None else ()
        anchor_map = await resolve_message_anchors(self.guild_id, self.channel_id, reply, role_ids=role_ids)
        thread_map = await resolve_thread_anchors(
            self.guild_id,
            self.channel_id,
            reply,
            known_channels=deps.forum_thread_channels if deps is not None else None,
            role_ids=role_ids,
        )
        self.reply_msg_id = await send_reply(
            self.kook,
            channel_id=self.channel_id,
            guild_id=self.guild_id,
            user_id=self.user_id,
            reply_to=quote,
            reply=reply,
            meta_text=meta_text,
            cfg=cfg,
            self_id=ident.self_id,
            nickname=ident.nickname,
            images=images,
            attached=deps.images if deps is not None else (),
            channel_map=self._channel_map(),
            message_anchor_map=anchor_map or None,
            thread_channel_map=thread_map or None,
            force_card=is_structured_report,
            theme=card_theme,
            chat_code=self.chat_code,
        )
        # If triggered by another KOOK bot, refresh cooldown from reply completion so slow long replies cannot loop.
        incoming = getattr(self, "incoming", None)
        if incoming is not None and incoming.sender.is_bot and self.channel_id is not None:
            from yggdrasil.triggers.bots import touch_bot_cooldown

            await touch_bot_cooldown(self.channel_id, self.user_id, cfg.bot_cooldown_s)
