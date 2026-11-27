"""Tests for the GMod bridge HTTP server, Redis transport, and ops communication tools."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from yggdrasil.gmod import bridge as gmod_bridge
from yggdrasil.gmod import server as gmod_server
from yggdrasil.kook.client import Kook
from yggdrasil.ops import tools as ops_tools_mod
from yggdrasil.ops.tools import OpsDeps, gmod_ping, gmod_servers
from yggdrasil.runtime import GModServerInfo


class _FakeRedis:
    """Minimal in-memory async Redis supporting strings and blocking list queues."""

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self._lists: dict[str, deque[str]] = {}
        self._waiters: dict[str, list[asyncio.Event]] = {}

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        del ex
        self._kv[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def delete(self, key: str) -> int:
        existed = key in self._kv or key in self._lists
        self._kv.pop(key, None)
        self._lists.pop(key, None)
        return 1 if existed else 0

    async def expire(self, key: str, ttl: int) -> bool:
        del key, ttl
        return True

    async def rpush(self, key: str, value: str) -> int:
        q = self._lists.setdefault(key, deque())
        q.append(value)
        for ev in self._waiters.get(key, ()):
            ev.set()
        return len(q)

    async def lpop(self, key: str) -> str | None:
        q = self._lists.get(key)
        if not q:
            return None
        return q.popleft()

    async def blpop(self, keys: list[str], *, timeout: int = 0) -> tuple[str, str] | None:  # noqa: ASYNC109
        for k in keys:
            item = await self.lpop(k)
            if item is not None:
                return (k, item)
        ev = asyncio.Event()
        for k in keys:
            self._waiters.setdefault(k, []).append(ev)
        try:
            wait = float(timeout) if timeout > 0 else 0.05
            await asyncio.wait_for(ev.wait(), timeout=wait)
        except TimeoutError:
            return None
        finally:
            for k in keys:
                lst = self._waiters.get(k)
                if lst and ev in lst:
                    lst.remove(ev)
        for k in keys:
            item = await self.lpop(k)
            if item is not None:
                return (k, item)
        return None


@pytest.fixture
def gmod_env(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake_redis = _FakeRedis()
    servers = {
        "main": GModServerInfo(id="main", name="主服", enabled=True, token="tok-main-secret", note="同机内网"),
        "surf": GModServerInfo(id="surf", name="滑翔服", enabled=True, token="tok-surf-secret", note="公网反代"),
        "off": GModServerInfo(id="off", name="停用服", enabled=False, token="tok-off-secret", note=""),
    }
    snap = SimpleNamespace(gmod_servers=servers)
    monkeypatch.setattr(gmod_bridge, "redis_client", lambda: fake_redis)
    monkeypatch.setattr(gmod_bridge, "snapshot", lambda: snap)
    monkeypatch.setattr(gmod_server, "snapshot", lambda: snap)
    monkeypatch.setattr(ops_tools_mod, "snapshot", lambda: snap)
    return fake_redis


async def test_bridge_auth_and_heartbeat(gmod_env: _FakeRedis) -> None:
    app = gmod_server.build_gmod_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
        health = await c.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        # Missing / invalid auth / unknown / disabled servers are rejected
        assert (await c.post("/bridge/gmod/poll", json={"server_id": "main", "wait_s": 0})).status_code == 401
        assert (
            await c.post(
                "/bridge/gmod/poll",
                json={"server_id": "main", "wait_s": 0},
                headers={"Authorization": "Basic abc"},
            )
        ).status_code == 401
        assert (
            await c.post(
                "/bridge/gmod/poll",
                json={"server_id": "main", "wait_s": 0},
                headers={"Authorization": "Bearer wrong-token"},
            )
        ).status_code == 401
        assert (
            await c.post(
                "/bridge/gmod/poll",
                json={"server_id": "unknown", "wait_s": 0},
                headers={"Authorization": "Bearer tok-main-secret"},
            )
        ).status_code == 401
        assert (
            await c.post(
                "/bridge/gmod/poll",
                json={"server_id": "off", "wait_s": 0},
                headers={"Authorization": "Bearer tok-off-secret"},
            )
        ).status_code == 401

        # Valid poll records heartbeat metadata, handlers, and forwarded remote IP
        resp = await c.post(
            "/bridge/gmod/poll",
            json={
                "server_id": "main",
                "wait_s": 0,
                "protocol_version": "1",
                "handlers": ["ping", "echo", "ping"],
                "meta": {"map": "gm_construct", "players": 4, "max_players": 32},
            },
            headers={
                "Authorization": "Bearer tok-main-secret",
                "X-Forwarded-For": "172.18.0.5, 127.0.0.1",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["jobs"] == []

        hb = await gmod_bridge.get_heartbeat("main")
        assert hb is not None
        assert hb.online is True
        assert hb.remote_ip == "172.18.0.5"
        assert hb.protocol_version == "1"
        assert hb.handlers == ("ping", "echo")
        assert hb.meta["map"] == "gm_construct"


async def test_bridge_roundtrip_and_cross_server_spoofing_protection(gmod_env: _FakeRedis) -> None:
    await gmod_bridge.record_heartbeat("main", handlers=["ping", "echo"], meta={"map": "gm_flatgrass"})
    app = gmod_server.build_gmod_app()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
        call_task = asyncio.create_task(gmod_bridge.call("main", "ping", {"source": "test"}, timeout_s=5.0))

        # GMod server polls and receives the enqueued ping job
        poll_resp = await c.post(
            "/bridge/gmod/poll",
            json={"server_id": "main", "wait_s": 2, "handlers": ["ping", "echo"]},
            headers={"Authorization": "Bearer tok-main-secret"},
        )
        assert poll_resp.status_code == 200
        jobs = poll_resp.json()["jobs"]
        assert len(jobs) == 1
        job = jobs[0]
        assert job["action"] == "ping"
        assert job["args"] == {"source": "test"}
        job_id = job["job_id"]

        # Another authenticated GMod server ("surf") CANNOT spoof a result for "main"'s job
        spoof = await c.post(
            "/bridge/gmod/result",
            json={"server_id": "surf", "job_id": job_id, "ok": True, "data": {"spoofed": True}},
            headers={"Authorization": "Bearer tok-surf-secret"},
        )
        assert spoof.status_code == 404

        # Legitimate server posts result
        res_resp = await c.post(
            "/bridge/gmod/result",
            json={"server_id": "main", "job_id": job_id, "ok": True, "data": {"pong": True, "map": "gm_flatgrass"}},
            headers={"Authorization": "Bearer tok-main-secret"},
        )
        assert res_resp.status_code == 200

        # Replay of the same job_id is rejected
        replay = await c.post(
            "/bridge/gmod/result",
            json={"server_id": "main", "job_id": job_id, "ok": True, "data": {}},
            headers={"Authorization": "Bearer tok-main-secret"},
        )
        assert replay.status_code == 404

        outcome = await call_task
        assert outcome.ok is True
        assert outcome.server_id == "main"
        assert outcome.action == "ping"
        assert outcome.data == {"pong": True, "map": "gm_flatgrass"}


async def test_bridge_call_error_cases(gmod_env: _FakeRedis) -> None:
    with pytest.raises(gmod_bridge.GModServerNotFound):
        await gmod_bridge.call("missing", "ping")
    with pytest.raises(gmod_bridge.GModServerNotFound):
        await gmod_bridge.call("off", "ping")
    with pytest.raises(gmod_bridge.GModServerOffline):
        await gmod_bridge.call("main", "ping")

    await gmod_bridge.record_heartbeat("main", handlers=["ping"])

    # Remote error propagation
    async def _reply_error() -> None:
        jobs = await gmod_bridge.pop_jobs("main", wait_s=2.0)
        assert len(jobs) == 1
        ok = await gmod_bridge.submit_result("main", jobs[0]["job_id"], ok=False, error="unknown action: boom")
        assert ok is True

    worker = asyncio.create_task(_reply_error())
    with pytest.raises(gmod_bridge.GModRemoteError, match="unknown action: boom"):
        await gmod_bridge.call("main", "boom", timeout_s=3.0, raise_on_error=True)
    await worker


@pytest.mark.parametrize("with_fresh_job", [False, True])
async def test_bridge_discards_timed_out_undelivered_jobs(gmod_env: _FakeRedis, with_fresh_job: bool) -> None:
    await gmod_bridge.record_heartbeat("main", handlers=["ncg.queue.letin", "ping"])
    with pytest.raises(gmod_bridge.GModTimeout, match=r"超时（1s）"):
        await gmod_bridge.call("main", "ncg.queue.letin", {"steamid": "STEAM_0:1:1"}, timeout_s=0.6)

    if not with_fresh_job:
        assert await gmod_bridge.pop_jobs("main", wait_s=0) == []
        return

    async def reply_to_fresh_job() -> None:
        (job,) = await gmod_bridge.pop_jobs("main", wait_s=2)
        assert job["action"] == "ping"
        assert "expires_at" not in job  # The deadline is internal to the Bot.
        assert await gmod_bridge.submit_result("main", job["job_id"], ok=True)

    async with asyncio.TaskGroup() as group:
        group.create_task(reply_to_fresh_job())
        outcome = await gmod_bridge.call("main", "ping", timeout_s=3)
    assert outcome.ok


async def test_bridge_delivers_legacy_job_without_deadline(gmod_env: _FakeRedis) -> None:
    legacy = {"job_id": "legacy", "action": "ping", "args": {}, "created_at": 1.0}
    await gmod_env.rpush("ygg:gmod:q:main", json.dumps(legacy))
    assert await gmod_bridge.pop_jobs("main", wait_s=0) == [legacy]


async def test_ops_gmod_tools(gmod_env: _FakeRedis) -> None:
    await gmod_bridge.record_heartbeat("main", handlers=["echo", "ping"], meta={"map": "rp_downtown", "players": 12})
    deps = OpsDeps(kook=cast(Kook, None), user_id="10001", channel_id="ch1")
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    listed = json.loads(await gmod_servers(ctx))
    by_id = {row["id"]: row for row in listed}
    assert by_id["main"]["online"] is True
    assert by_id["main"]["handlers"] == ["echo", "ping"]
    assert by_id["main"]["meta"]["map"] == "rp_downtown"
    assert by_id["surf"]["online"] is False

    # Offline server is formatted as an ops tool error string by @_op
    offline_msg = await gmod_ping(ctx, "surf")
    assert offline_msg.startswith("[失败]")
    assert "离线" in offline_msg

    # Online ping round-trip
    async def _pong() -> None:
        jobs = await gmod_bridge.pop_jobs("main", wait_s=2.0)
        assert jobs
        assert jobs[0]["action"] == "ping"
        await gmod_bridge.submit_result("main", jobs[0]["job_id"], ok=True, data={"pong": True})

    worker = asyncio.create_task(_pong())
    ping_out = json.loads(await gmod_ping(ctx, "main"))
    await worker
    assert ping_out["server_id"] == "main"
    assert ping_out["data"] == {"pong": True}
