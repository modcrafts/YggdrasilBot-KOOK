"""Tools of the ops agent (superusers only): 4-level config management, KOOK sync, review, and KOOK API v3."""

import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel
from pydantic_ai import RunContext, Tool
from sqlalchemy import func, select

from yggdrasil.admin import channels as channels_api
from yggdrasil.admin import kook as kook_api_admin
from yggdrasil.admin import reviews as reviews_api
from yggdrasil.admin import routes as routes_api
from yggdrasil.admin import settings_api
from yggdrasil.admin import triggers as triggers_api
from yggdrasil.core.log import LOGGER
from yggdrasil.core.metrics import TOOL_CALLS, TOOL_DURATION
from yggdrasil.db.models import (
    BotEvent,
    Channel,
    CommandTrigger,
    ConfigAudit,
    Guild,
    Invocation,
    Provider,
    ReviewItem,
    TaskRoute,
)
from yggdrasil.db.session import get_session
from yggdrasil.kook.client import Kook
from yggdrasil.kook.model import KookError
from yggdrasil.kv import redis_client
from yggdrasil.people import label, names_in_guilds, self_label
from yggdrasil.runtime import snapshot
from yggdrasil.settings import Settings
from yggdrasil.tools.errors import ToolError
from yggdrasil.transcript.notes import TraceNotes

_UNDO_KEEP = 20
_UNDO_TTL_S = 7 * 86400

ScopeLevel = Literal["global", "guild", "category", "channel"]


@dataclass
class QuotedMessage:
    kook_message_id: str
    is_self: bool
    text: str


@dataclass
class OpsDeps:
    kook: Kook
    user_id: str
    channel_id: str | None
    guild_id: str | None = None
    quoted: QuotedMessage | None = None
    actions: list[str] = field(default_factory=list)
    tool_counts: Counter[str] = field(default_factory=Counter)
    notes: TraceNotes = field(default_factory=TraceNotes)

    @property
    def actor(self) -> str:
        return f"kook:{self.user_id}"


async def _labeler(
    ctx: RunContext[OpsDeps], pairs: list[tuple[str | None, str | None]]
) -> Callable[[str | None, str | None], str | None]:
    names = await names_in_guilds(pairs)
    self_id = ctx.deps.kook.self_id
    me = self_label(snapshot().settings().persona.name)

    def who(guild_id: str | None, user_id: str | None) -> str | None:
        if not user_id:
            return user_id
        if user_id == self_id:
            return me
        return label(names.get((guild_id, user_id)) if guild_id else None, user_id)

    return who


def _op[**P](name: str, *, write: bool = False) -> Callable[[Callable[P, Awaitable[str]]], Callable[P, Awaitable[str]]]:
    def deco(fn: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
            ctx = args[0]
            assert isinstance(ctx, RunContext)
            ctx.deps.tool_counts[name] += 1
            started, outcome = time.perf_counter(), "ok"
            try:
                result = await fn(*args, **kwargs)
                if write:
                    ctx.deps.actions.append(result)
                return result
            except HTTPException as e:
                outcome = "error"
                ctx.deps.notes.failed(ctx.tool_call_id)
                return f"[失败] {_detail(e.detail)}"
            except ToolError as e:
                outcome = "error"
                ctx.deps.notes.failed(ctx.tool_call_id)
                return f"[失败] {e}"
            except KookError as e:
                outcome = "error"
                ctx.deps.notes.failed(ctx.tool_call_id)
                return f"[失败] KOOK：{str(e)[:300]}"
            except Exception as e:
                outcome = "error"
                ctx.deps.notes.failed(ctx.tool_call_id)
                LOGGER.opt(exception=e).warning("ops tool {} failed", name)
                return f"[失败] {type(e).__name__}: {str(e)[:300]}"
            finally:
                TOOL_CALLS.add(1, {"tool": f"ops.{name}", "outcome": outcome})
                TOOL_DURATION.record(time.perf_counter() - started, {"tool": f"ops.{name}"})

        return wrapper

    return deco


def _detail(detail: Any) -> str:
    if isinstance(detail, list):
        return "；".join(f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
    return str(detail)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# --- undo stack ----------------------------------------------------------------


def _undo_key(user_id: str) -> str:
    return f"ygg:ops:undo:{user_id}"


async def _push_undo(deps: OpsDeps, title: str, step: dict[str, Any]) -> None:
    r = redis_client()
    key = _undo_key(deps.user_id)
    await r.lpush(key, _json({"label": title, **step}))
    await r.ltrim(key, 0, _UNDO_KEEP - 1)
    await r.expire(key, _UNDO_TTL_S)


def merge_patch(doc: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """RFC 7386-style: nested dicts merge, ``None`` removes the key (back to inherited value)."""
    out = dict(doc)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            merged = merge_patch(out[key], value)
            if merged:
                out[key] = merged
            else:
                out.pop(key)
        else:
            out[key] = value
    return out


# --- read ----------------------------------------------------------------------


@_op("status")
async def status(ctx: RunContext[OpsDeps]) -> str:
    """当前配置版本、服务器和启用频道数量、已配置的路由、待审核数量。"""
    snap = snapshot()
    async with get_session() as s:
        pending = await s.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "pending"))
    enabled = [
        f"#{c.name}({c.channel_id})"
        for c in snap.channels.values()
        if not c.is_category and snap.channel_enabled(c.channel_id)
    ]
    return _json(
        {
            "config_version": snap.version,
            "guilds": len(snap.guilds),
            "enabled_channels": enabled,
            "routes": sorted(snap.routes),
            "pending_reviews": pending,
        }
    )


@_op("get_settings")
async def get_settings(
    ctx: RunContext[OpsDeps],
    scope: ScopeLevel = "global",
    scope_id: str | None = None,
    section: str | None = None,
) -> str:
    """查看四级设置（global / guild / category / channel）。section 指定一节时额外返回生效值。"""
    if scope == "global":
        view = await settings_api.get_global_settings()
    else:
        if not scope_id:
            raise ToolError(f"{scope} 级设置必须提供 scope_id")
        view = await settings_api.get_scope_settings(scope, scope_id)
    out: dict[str, Any] = {"scope": scope, "scope_id": scope_id, "chain": view.chain, "stored": view.value}
    if section:
        if section not in Settings.model_fields:
            raise ToolError(f"没有 {section} 这一节，可选：{', '.join(Settings.model_fields)}")
        out["effective"] = {section: view.effective.get(section)}
    return _json(out)


@_op("settings_reference")
async def settings_reference(ctx: RunContext[OpsDeps], section: str) -> str:
    """某一节设置有哪些字段：类型、默认值、说明。改设置前先查字段名。"""
    info = Settings.model_fields.get(section)
    model = info.annotation if info else None
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ToolError(f"没有 {section} 这一节，可选：{', '.join(Settings.model_fields)}")
    defaults = model().model_dump(mode="json")
    return _json(
        {
            name: {
                "type": str(f.annotation).replace("typing.", ""),
                "default": defaults.get(name),
                "desc": f.description,
            }
            for name, f in model.model_fields.items()
        }
    )


@_op("list_channels")
async def list_channels(ctx: RunContext[OpsDeps]) -> str:
    """查看所有服务器 → 分组 → 频道树结构、启用状态与覆盖摘要。"""
    tree = await channels_api.get_channel_tree()
    return _json(
        [
            {
                "guild_id": g.guild.guild_id,
                "name": g.guild.name,
                "enabled": g.guild.enabled,
                "overrides": list(g.guild.settings),
                "categories": [
                    {
                        "category_id": cat.category.channel_id,
                        "name": cat.category.name,
                        "enabled": cat.category.enabled,
                        "overrides": list(cat.category.settings),
                        "channels": [
                            {
                                "channel_id": ch.channel_id,
                                "name": ch.name,
                                "enabled": ch.enabled,
                                "effective_enabled": ch.effective_enabled,
                                "overrides": list(ch.settings),
                            }
                            for ch in cat.channels
                        ],
                    }
                    for cat in g.categories
                ],
                "ungrouped": [
                    {
                        "channel_id": ch.channel_id,
                        "name": ch.name,
                        "enabled": ch.enabled,
                        "effective_enabled": ch.effective_enabled,
                        "overrides": list(ch.settings),
                    }
                    for ch in g.ungrouped
                ],
            }
            for g in tree
        ]
    )


@_op("list_routes")
async def list_routes(ctx: RunContext[OpsDeps]) -> str:
    """每个任务用的 provider、模型和兜底模型，以及可用的 provider 和它们的模型列表。"""
    snap = snapshot()
    names = {p.id: p.name for p in snap.providers.values()}
    routes = {
        t: {
            "provider": names.get(r.provider_id),
            "model": r.model,
            "fallback": f"{names.get(r.fallback_provider_id or 0)}/{r.fallback_model}" if r.fallback_model else None,
        }
        for t, r in snap.routes.items()
    }
    providers = {p.name: list(p.models) for p in snap.providers.values() if p.enabled}
    return _json({"routes": routes, "providers": providers})


@_op("list_triggers")
async def list_triggers(ctx: RunContext[OpsDeps]) -> str:
    """所有触发规则（id、handler、scope、scope_id、匹配方式、关键词、优先级、是否启用）。"""
    rows = await triggers_api.list_triggers()
    return _json(
        [
            t.model_dump(
                include={
                    "id",
                    "handler",
                    "scope",
                    "scope_id",
                    "match_type",
                    "patterns",
                    "priority",
                    "enabled",
                    "allowed_roles",
                    "note",
                }
            )
            for t in rows
        ]
    )


@_op("list_reviews")
async def list_reviews(
    ctx: RunContext[OpsDeps], status: Literal["pending", "approved", "rejected", "expired"] = "pending"
) -> str:
    """审核队列（默认只看待审核）：R 号、频道、请求、候选回复开头、审核结果。"""
    page = await reviews_api.list_reviews(status=status, limit=20, offset=0)
    return _json(
        [
            {
                "id": f"R{r.id}",
                "channel_id": r.channel_id,
                "guild_id": r.guild_id,
                "request": r.request_text[:200],
                "reply": " ".join(s.content for s in r.reply.sections)[:200],
                "verdict": r.verdict.model_dump(include={"verdict", "score", "categories"}),
            }
            for r in page.items
        ]
    )


@_op("recent_invocations")
async def recent_invocations(ctx: RunContext[OpsDeps], limit: int = 10) -> str:
    """最近的调用记录：时间、频道、触发人、handler、状态、耗时、错误。"""
    async with get_session() as s:
        rows = (
            await s.scalars(select(Invocation).order_by(Invocation.created_at.desc()).limit(min(max(limit, 1), 30)))
        ).all()
    who = await _labeler(ctx, [(i.guild_id, i.user_id) for i in rows])
    return _json(
        [
            {
                "at": i.created_at.astimezone().strftime("%m-%d %H:%M"),
                "channel_id": i.channel_id,
                "guild_id": i.guild_id,
                "user": who(i.guild_id, i.user_id),
                "handler": i.handler,
                "status": i.status,
                "ms": i.duration_ms,
                "error": (i.error or "")[:200] or None,
            }
            for i in rows
        ]
    )


# --- write ---------------------------------------------------------------------


@_op("update_settings", write=True)
async def update_settings(
    ctx: RunContext[OpsDeps],
    patch: dict[str, Any],
    scope: ScopeLevel = "global",
    scope_id: str | None = None,
) -> str:
    """修改四级设置（global / guild / category / channel）。值写 null 表示删除该层覆盖恢复继承上级。"""
    if scope == "global":
        before = await settings_api.global_doc()
        after = merge_patch(before, patch)
        await settings_api.put_global_settings(after, ctx.deps.actor)
        tag = "全局"
    else:
        if not scope_id:
            raise ToolError(f"{scope} 级设置必须提供 scope_id")
        view = await settings_api.get_scope_settings(scope, scope_id)
        before = dict(view.value)
        after = merge_patch(before, patch)
        await settings_api.put_scope_settings(scope, scope_id, after, ctx.deps.actor)
        tag = f"{scope} {scope_id} "
    await _push_undo(
        ctx.deps,
        f"{tag}设置",
        {"kind": "settings", "scope": scope, "scope_id": scope_id, "doc": before},
    )
    return f"已修改{tag}设置：{_json(patch)}"


@_op("set_channel", write=True)
async def set_channel(
    ctx: RunContext[OpsDeps],
    channel_id: str,
    enabled: bool | None = None,
    clear_enabled: bool = False,
    name: str | None = None,
    guild_id: str | None = None,
) -> str:
    """启用或停用某个频道/分组（clear_enabled=True 表示设为 null 继承所属分组）。"""
    async with get_session() as s:
        ch = await s.get(Channel, channel_id)
        prev = (
            {
                "guild_id": ch.guild_id,
                "parent_id": ch.parent_id,
                "name": ch.name,
                "topic": ch.topic,
                "position": ch.position,
                "is_category": ch.is_category,
                "channel_type": ch.channel_type,
                "enabled": ch.enabled,
            }
            if ch
            else None
        )
    if prev is None and not guild_id:
        raise ToolError(f"频道 {channel_id} 不在列表里，请先执行 sync_kook 或传入 guild_id")
    next_enabled = None if clear_enabled else (enabled if enabled is not None else (prev or {}).get("enabled"))
    body = channels_api.ChannelIn(
        guild_id=guild_id or (prev or {})["guild_id"],
        parent_id=(prev or {}).get("parent_id"),
        name=name if name is not None else (prev or {}).get("name", ""),
        topic=(prev or {}).get("topic", ""),
        position=(prev or {}).get("position", 0),
        is_category=bool((prev or {}).get("is_category", False)),
        channel_type=(prev or {}).get("channel_type"),
        enabled=next_enabled,
        settings=None,
    )
    out = await channels_api.upsert_channel(channel_id, body, ctx.deps.actor)
    await _push_undo(ctx.deps, f"频道 {channel_id}", {"kind": "channel", "channel_id": channel_id, "prev": prev})
    state = "继承分组" if out.enabled is None else ("启用" if out.enabled else "停用")
    return f"频道 #{out.name}（{out.channel_id}）：{state}（实际生效：{'启用' if out.effective_enabled else '停用'}）"


@_op("set_guild", write=True)
async def set_guild(
    ctx: RunContext[OpsDeps], guild_id: str, enabled: bool | None = None, name: str | None = None
) -> str:
    """启用或停用整个 KOOK 服务器。"""
    async with get_session() as s:
        g = await s.get(Guild, guild_id)
        prev = {"name": g.name, "icon": g.icon, "enabled": g.enabled} if g else None
    body = channels_api.GuildIn(
        name=name if name is not None else (prev or {}).get("name", guild_id),
        icon=(prev or {}).get("icon", ""),
        enabled=enabled if enabled is not None else bool(prev is None or prev["enabled"]),
        settings=None,
    )
    out = await channels_api.upsert_guild(guild_id, body, ctx.deps.actor)
    await _push_undo(ctx.deps, f"服务器 {guild_id}", {"kind": "guild", "guild_id": guild_id, "prev": prev})
    return f"服务器「{out.name}」（{out.guild_id}）：{'启用' if out.enabled else '停用'}"


@_op("sync_kook", write=True)
async def sync_kook(ctx: RunContext[OpsDeps]) -> str:
    """从 KOOK 同步所有服务器、分组和文字频道到数据库（保留已有设置与开关）。"""
    res = await kook_api_admin.sync_kook_structure(ctx.deps.actor)
    return f"已同步 {res.guilds} 个服务器、{res.categories} 个分组、{res.channels} 个文字频道"


@_op("backfill_channel", write=True)
async def backfill_channel(ctx: RunContext[OpsDeps], channel_id: str) -> str:
    """回填一个文字频道的历史消息（后台进行）。不能撤销。"""
    await channels_api.backfill_channel(channel_id, ctx.deps.actor)
    return f"已开始回填频道 {channel_id}"


@_op("decide_review", write=True)
async def decide_review(
    ctx: RunContext[OpsDeps],
    action: Literal["approve", "reject", "edit"],
    review_id: int | None = None,
    text: str | None = None,
) -> str:
    """处理审核：approve 通过并发送原回复，reject 拒绝，edit 把 text 作为回复发出。不能撤销。"""
    if review_id is None and ctx.deps.quoted and (m := re.search(r"R(\d+)", ctx.deps.quoted.text)):
        review_id = int(m.group(1))
    if review_id is None:
        page = await reviews_api.list_reviews(status="pending", limit=5, offset=0)
        if page.total != 1:
            raise ToolError(f"待审核有 {page.total} 条，请说明是哪一条（R 号）")
        review_id = page.items[0].id
    out = await reviews_api.decide_review(review_id, reviews_api.DecideIn(action=action, text=text), ctx.deps.actor)
    return f"R{out.id} → {out.status}"


class TriggerSpec(triggers_api.TriggerIn):
    """Same fields and checks as the dashboard's trigger form."""


@_op("save_trigger", write=True)
async def save_trigger(ctx: RunContext[OpsDeps], trigger: TriggerSpec, trigger_id: int | None = None) -> str:
    """新增（trigger_id 为空）或修改一条触发规则。修改时传完整规则。"""
    prev = None
    if trigger_id is not None:
        async with get_session() as s:
            row = await s.get(CommandTrigger, trigger_id)
            if row is None:
                raise ToolError(f"规则 {trigger_id} 不存在")
            prev = triggers_api.TriggerIn.model_validate(row, from_attributes=True).model_dump()
        out = await triggers_api.update_trigger(trigger_id, trigger, ctx.deps.actor)
    else:
        out = await triggers_api.create_trigger(trigger, ctx.deps.actor)
    await _push_undo(ctx.deps, f"触发规则 {out.id}", {"kind": "trigger", "id": out.id, "prev": prev})
    return f"规则 {out.id} 已保存"


@_op("delete_trigger", write=True)
async def delete_trigger(ctx: RunContext[OpsDeps], trigger_id: int) -> str:
    """删除一条触发规则。"""
    async with get_session() as s:
        row = await s.get(CommandTrigger, trigger_id)
        if row is None:
            raise ToolError(f"规则 {trigger_id} 不存在")
        prev = triggers_api.TriggerIn.model_validate(row, from_attributes=True).model_dump()
    await triggers_api.delete_trigger(trigger_id, ctx.deps.actor)
    await _push_undo(ctx.deps, f"删除规则 {trigger_id}", {"kind": "trigger", "id": None, "prev": prev})
    return f"规则 {trigger_id} 已删除"


async def _provider_id(name: str) -> int:
    async with get_session() as s:
        pid = await s.scalar(select(Provider.id).where(Provider.name == name))
    if pid is None:
        raise ToolError(f"没有叫 {name} 的 provider")
    return pid


@_op("set_route", write=True)
async def set_route(
    ctx: RunContext[OpsDeps],
    task: str,
    provider: str,
    model: str,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
) -> str:
    """更换某个任务的模型（provider 用名称）。"""
    async with get_session() as s:
        row = await s.get(TaskRoute, task)
        prev = (
            routes_api.RouteIn(
                provider_id=row.provider_id,
                model=row.model,
                params=dict(row.params),
                fallback_provider_id=row.fallback_provider_id,
                fallback_model=row.fallback_model,
                limits=routes_api.RouteLimits(**row.limits),
            ).model_dump()
            if row
            else None
        )
    body = routes_api.RouteIn(
        provider_id=await _provider_id(provider),
        model=model,
        params=(prev or {}).get("params", {}),
        fallback_provider_id=await _provider_id(fallback_provider) if fallback_provider and fallback_model else None,
        fallback_model=fallback_model if fallback_provider else None,
        limits=routes_api.RouteLimits(**(prev or {}).get("limits", {})),
    )
    await routes_api.upsert_route(task, body, ctx.deps.actor)
    await _push_undo(ctx.deps, f"路由 {task}", {"kind": "route", "task": task, "prev": prev})
    return f"{task} → {provider}/{model}"


@_op("undo_last_change", write=True)
async def undo_last_change(ctx: RunContext[OpsDeps]) -> str:
    """撤销你最近一次通过对话做的配置修改。"""
    raw = await redis_client().lpop(_undo_key(ctx.deps.user_id))
    if raw is None:
        raise ToolError("没有可以撤销的修改")
    step = json.loads(str(raw))
    actor, kind = ctx.deps.actor, step["kind"]
    if kind == "settings":
        if step["scope"] == "global":
            await settings_api.put_global_settings(step["doc"], actor)
        else:
            await settings_api.put_scope_settings(step["scope"], step["scope_id"], step["doc"], actor)
    elif kind == "channel":
        if step["prev"] is None:
            await channels_api.delete_channel(step["channel_id"], actor)
        else:
            await channels_api.upsert_channel(step["channel_id"], channels_api.ChannelIn(**step["prev"]), actor)
    elif kind == "guild":
        if step["prev"] is None:
            await channels_api.delete_guild(step["guild_id"], actor)
        else:
            await channels_api.upsert_guild(step["guild_id"], channels_api.GuildIn(**step["prev"]), actor)
    elif kind == "trigger":
        if step["prev"] is None:
            await triggers_api.delete_trigger(step["id"], actor)
        elif step["id"] is None:
            await triggers_api.create_trigger(triggers_api.TriggerIn(**step["prev"]), actor)
        else:
            await triggers_api.update_trigger(step["id"], triggers_api.TriggerIn(**step["prev"]), actor)
    elif kind == "route":
        if step["prev"] is None:
            await routes_api.delete_route(step["task"], actor)
        else:
            await routes_api.upsert_route(step["task"], routes_api.RouteIn(**step["prev"]), actor)
    return f"已撤销：{step['label']}"


@_op("recall_message", write=True)
async def recall_message(ctx: RunContext[OpsDeps]) -> str:
    """删除/撤回被回复的那条 bot 消息。"""
    q = ctx.deps.quoted
    if q is None or not q.is_self:
        raise ToolError("没有回复 bot 发的消息")
    await ctx.deps.kook.recall(channel_id=ctx.deps.channel_id, msg_id=q.kook_message_id)
    return "已删除"


@_op("list_events")
async def list_events(
    ctx: RunContext[OpsDeps],
    kind: str | None = None,
    guild_id: str | None = None,
    limit: int = 20,
) -> str:
    """bot 记录的 KOOK 系统事件（服务器/频道变动等）。"""
    async with get_session() as s:
        q = select(BotEvent).order_by(BotEvent.at.desc()).limit(min(max(limit, 1), 50))
        if kind:
            q = q.where(BotEvent.kind.startswith(kind))
        if guild_id:
            q = q.where(BotEvent.guild_id == guild_id)
        rows = (await s.scalars(q)).all()
    who = await _labeler(ctx, [(e.guild_id, u) for e in rows for u in (e.user_id, e.operator_id)])
    return _json(
        [
            {
                "id": f"E{e.id}",
                "at": e.at.astimezone().strftime("%m-%d %H:%M"),
                "kind": e.kind,
                "guild_id": e.guild_id,
                "channel_id": e.channel_id,
                "user": who(e.guild_id, e.user_id),
                "operator": who(e.guild_id, e.operator_id),
                "status": e.status,
            }
            for e in rows
        ]
    )


_KOOK_NEVER = frozenset({"/guild/leave", "/guild/delete", "/user/offline"})
_KOOK_MAX_CHARS = 8000


def kook_gate(method: str, path: str, write_allowed: list[str]) -> bool:
    """Return True if ``(method, path)`` is an allowed write, False if read; raise ToolError otherwise."""
    m = method.upper().strip()
    p = "/" + path.strip().lstrip("/")
    if p in _KOOK_NEVER:
        raise ToolError(f"{p} 不允许通过对话调用")
    if m == "GET":
        return False
    if m != "POST":
        raise ToolError(f"KOOK API 只支持 GET 或 POST，不支持 {m}")
    if p not in write_allowed:
        raise ToolError(f"{p} 不在允许的写接口里（设置 ops.kook_write_actions）")
    return True


@_op("kook_api")
async def kook_api(
    ctx: RunContext[OpsDeps],
    path: str,
    method: Literal["GET", "POST"] = "GET",
    params: dict[str, Any] | None = None,
) -> str:
    """直接调用 KOOK API v3 接口（例如 GET /guild/list、GET /channel/list、POST /message/pin）。
    GET 只读接口默认放行；POST 写接口只能调 ops.kook_write_actions 列出的路径，记入审计且不可撤销。"""
    p = "/" + path.strip().lstrip("/")
    write = kook_gate(method, p, snapshot().settings().ops.kook_write_actions)
    params = params or {}
    result = await ctx.deps.kook.call(method, p, params)
    if write:
        async with get_session() as s:
            s.add(ConfigAudit(actor=ctx.deps.actor, entity="kook", entity_id=p, action="call", detail=params))
        ctx.deps.actions.append(f"已调用 {method} {p}")
    text = _json(result)
    if len(text) > _KOOK_MAX_CHARS:
        text = text[:_KOOK_MAX_CHARS] + f"…（已截断，共 {len(text)} 字符）"
    return text


_READ: tuple[Callable[..., Awaitable[str]], ...] = (
    status,
    get_settings,
    settings_reference,
    list_channels,
    list_routes,
    list_triggers,
    list_reviews,
    recent_invocations,
    kook_api,
    list_events,
)
_WRITE: tuple[Callable[..., Awaitable[str]], ...] = (
    update_settings,
    set_channel,
    set_guild,
    sync_kook,
    backfill_channel,
    decide_review,
    save_trigger,
    delete_trigger,
    set_route,
    undo_last_change,
)


def ops_tools(deps: OpsDeps) -> list[Tool[OpsDeps]]:
    tools: list[Tool[OpsDeps]] = [Tool(fn, takes_ctx=True) for fn in (*_READ, *_WRITE)]
    if deps.quoted is not None and deps.quoted.is_self:
        tools.append(Tool(recall_message, takes_ctx=True))
    return tools
