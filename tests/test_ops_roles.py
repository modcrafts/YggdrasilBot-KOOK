"""Tests for ops agent per-KOOK-role authorization, tool filtering, and guild-scope security guards."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from tests.support.kook import FakeKook
from yggdrasil.kook.model import Incoming, Sender
from yggdrasil.ops import tools as ops_tools_mod
from yggdrasil.ops.tools import (
    ALL_OPS_TOOL_NAMES,
    OpsDeps,
    OpsGrant,
    kook_api,
    ops_tools,
    resolve_ops_grant,
    set_route,
    sync_kook,
    update_settings,
)
from yggdrasil.runtime import ChannelInfo, GuildInfo, Snapshot, TriggerRule
from yggdrasil.settings import OpsRoleRule, Settings
from yggdrasil.tools.errors import ToolError
from yggdrasil.triggers.index import MatchInput, TriggerIndex


def _incoming(
    user_id: str, *, channel_id: str | None = "ch-1", guild_id: str | None = "g-1", role_ids: tuple[int, ...] = ()
) -> Incoming:
    return Incoming(
        self_id="bot-1",
        message_id="msg-1",
        channel_id=channel_id,
        guild_id=guild_id,
        sender=Sender(user_id=user_id, name="Alice", role="member", role_ids=role_ids),
        text="ops status",
        segments=[],
        time=1_740_000_000,
    )


def test_resolve_ops_grant_and_tool_filtering() -> None:
    s = Settings()
    s.ops.kook_write_actions = ["/message/pin", "/message/unpin", "/guild/nickname"]
    s.ops.role_rules = {
        "101": OpsRoleRule(
            tools=["status", "list_channels", "kook_api", "sync_kook"],
            kook_write_actions=["/message/pin", "/guild/kickout", "/guild/leave"],
            guild_only=True,
        ),
        "102": OpsRoleRule(
            tools=["status", "sync_kook"],
            kook_write_actions=[],
            guild_only=False,
        ),
    }

    # Unauthorized regular member gets None
    assert resolve_ops_grant(s, _incoming("u-1", role_ids=(999,)), frozenset({"su-1"})) is None

    # Superuser gets all ops tools & full global kook_write_actions
    su_grant = resolve_ops_grant(s, _incoming("su-1", channel_id=None, guild_id=None), frozenset({"su-1"}))
    assert su_grant is not None
    assert su_grant.is_superuser is True
    assert su_grant.guild_only is False
    assert su_grant.allowed_tools == ALL_OPS_TOOL_NAMES
    assert "/message/pin" in su_grant.kook_write_actions

    # Guild-only role: strips global_only tool (sync_kook), intersects kook_write_actions with global whitelist
    mod_grant = resolve_ops_grant(s, _incoming("u-mod", role_ids=(101,)), frozenset({"su-1"}))
    assert mod_grant is not None
    assert mod_grant.is_superuser is False
    assert mod_grant.guild_only is True
    assert mod_grant.allowed_tools == frozenset({"status", "list_channels", "kook_api"})
    assert mod_grant.kook_write_actions == frozenset({"/message/pin"})

    # ops_tools() only exposes tools in grant.allowed_tools
    deps = OpsDeps(kook=FakeKook(), user_id="u-mod", channel_id="ch-1", guild_id="g-1", grant=mod_grant)
    exposed = {t.name for t in ops_tools(deps)}
    assert exposed == {"status", "list_channels", "kook_api"}

    # TriggerIndex allows matching default superuser-only admin.ops rule when ops_authorized=True
    snap = Snapshot(
        triggers=(
            TriggerRule(
                id=1,
                handler="admin.ops",
                scope="global",
                scope_id=None,
                match_type="regex",
                patterns=(r"^(?i:ops)(?![A-Za-z0-9_])",),
                arg_template=None,
                require_mention=True,
                target="quoted_or_self",
                priority=5,
                cooldown_s=0,
                allowed_roles=("superuser",),
            ),
        )
    )
    idx = TriggerIndex(snap)
    assert (
        idx.match(
            MatchInput(
                scope="channel",
                channel_id="ch-1",
                guild_id="g-1",
                text="ops 置顶",
                to_me=True,
                role="member",
                ops_authorized=False,
            )
        )
        is None
    )
    matched = idx.match(
        MatchInput(
            scope="channel",
            channel_id="ch-1",
            guild_id="g-1",
            text="ops 置顶",
            to_me=True,
            role="member",
            ops_authorized=True,
        )
    )
    assert matched is not None
    assert matched.handler == "admin.ops"
    assert matched.args == "置顶"


async def test_guild_only_security_guards_block_escalation_and_cross_guild(
    snapshot: Callable[..., Snapshot],
) -> None:
    snapshot(
        guilds={
            "g-1": GuildInfo("g-1", "Guild 1", "", {}, True),
            "g-2": GuildInfo("g-2", "Guild 2", "", {}, True),
        },
        channels={
            "ch-1": ChannelInfo("ch-1", "g-1", None, False, 0, "general", True, {}, 1),
            "ch-2": ChannelInfo("ch-2", "g-2", None, False, 0, "other", True, {}, 1),
        },
    )

    class _RecordingKook(FakeKook):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def call(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
            self.calls.append((method, path, dict(params or {})))
            return {"ok": True}

    kook = _RecordingKook()
    grant = OpsGrant(
        is_superuser=False,
        allowed_tools=frozenset({"update_settings", "kook_api", "sync_kook", "set_route"}),
        kook_write_actions=frozenset({"/message/pin", "/direct-message/create", "/guild/nickname"}),
        guild_only=True,
    )
    deps = OpsDeps(kook=kook, user_id="u-mod", channel_id="ch-1", guild_id="g-1", grant=grant)
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    # 1. Cannot update global settings, foreign guild, or ops section (anti-privilege-escalation)
    assert "[失败]" in await update_settings(ctx, {"features": {"chat": False}}, scope="global")
    assert "[失败]" in await update_settings(ctx, {"features": {"chat": False}}, scope="guild", scope_id="g-2")
    assert "[失败]" in await update_settings(ctx, {"features": {"chat": False}}, scope="channel", scope_id="ch-2")
    assert "ops 权限配置" in await update_settings(ctx, {"ops": {"role_rules": {}}}, scope="guild", scope_id="g-1")

    # 2. Cannot run global-only ops tools
    assert "[失败]" in await sync_kook(ctx)
    assert "[失败]" in await set_route(ctx, "chat", "OpenAI", "gpt-5")

    # 3. kook_api blocks DM writes, foreign guild_id, foreign target_id, and ungranted write paths
    assert "[失败]" in await kook_api(ctx, "/direct-message/create", "POST", {"target_id": "u-2", "content": "hi"})
    assert "[失败]" in await kook_api(ctx, "/guild/nickname", "POST", {"guild_id": "g-2", "user_id": "u-2"})
    assert "[失败]" in await kook_api(ctx, "/message/pin", "POST", {"target_id": "ch-2", "msg_id": "m-1"})
    assert "[失败]" in await kook_api(ctx, "/guild/kickout", "POST", {"guild_id": "g-1", "target_id": "u-2"})
    assert kook.calls == []

    # 4. kook_api auto-injects current guild_id for /guild/* writes and allows same-guild calls
    # (Skip ConfigAudit DB write by testing _assert_kook_write_in_guild directly)
    params: dict[str, Any] = {"user_id": "u-2", "nickname": "Test"}
    ops_tools_mod._assert_kook_write_in_guild(deps, "/guild/nickname", params)  # pyright: ignore[reportPrivateUsage]
    assert params["guild_id"] == "g-1"
    with pytest.raises(ToolError, match="不属于本服务器"):
        ops_tools_mod._assert_kook_write_in_guild(deps, "/message/pin", {"target_id": "ch-2"})  # pyright: ignore[reportPrivateUsage]
