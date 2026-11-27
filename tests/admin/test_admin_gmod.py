"""Unit/ASGI tests for ``/api/gmod/*`` (in-memory DB store, no live Postgres/Redis required)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from yggdrasil.admin import common as admin_common
from yggdrasil.admin import gmod_api
from yggdrasil.crypto import decrypt
from yggdrasil.db.models import SettingsDoc
from yggdrasil.gmod.bridge import GModCallResult, GModHeartbeat, GModServerOffline
from yggdrasil.runtime import GMOD_SERVERS_KEY


class _FakeSession:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    async def get(self, model: type[Any], key: Any) -> Any:
        if model is SettingsDoc:
            return self._store["settings"].get(key)
        return None

    def add(self, obj: Any) -> None:
        if isinstance(obj, SettingsDoc):
            self._store["settings"][obj.key] = obj
        else:
            self._store["audits"].append(obj)


@pytest.fixture
def fake_gmod_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    store: dict[str, Any] = {"settings": {}, "audits": [], "heartbeats": {}}

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession(store)

    async def _get_hb(server_id: str) -> GModHeartbeat | None:
        return store["heartbeats"].get(server_id)

    monkeypatch.setattr(gmod_api, "get_session", _session)
    monkeypatch.setattr(admin_common, "get_session", _session)
    monkeypatch.setattr(gmod_api, "get_heartbeat", _get_hb)
    monkeypatch.setattr(gmod_api, "bridge_serving_port", lambda: 8091)
    return store


async def test_gmod_servers_crud_and_token_encryption(
    client: httpx.AsyncClient, fake_gmod_env: dict[str, Any], published: list[str]
) -> None:
    # Initial empty list
    resp = await client.get("/api/gmod/servers")
    assert resp.status_code == 200, resp.text
    overview = resp.json()
    assert overview["serving"] is True
    assert overview["servers"] == []

    # Invalid server ID rejected
    bad = await client.post("/api/gmod/servers", json={"id": "Bad ID!", "name": "Bad"})
    assert bad.status_code == 422

    # Create server with auto-generated token
    created = await client.post(
        "/api/gmod/servers",
        json={"id": "main", "name": "主服 Sandbox", "enabled": True, "note": "同机 Wings 内网"},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    token1 = body["plaintext_token"]
    assert isinstance(token1, str)
    assert token1.startswith("ygg_gmod_")
    assert body["server"]["id"] == "main"
    assert body["server"]["token_masked"] is not None
    assert token1 not in body["server"]["token_masked"]

    # Stored encrypted in SettingsDoc("gmod.servers")
    raw_entry = fake_gmod_env["settings"][GMOD_SERVERS_KEY].value["servers"][0]
    assert raw_entry["token_enc"] != token1
    assert decrypt(raw_entry["token_enc"].encode()) == token1

    # Duplicate server ID returns 409
    dup = await client.post("/api/gmod/servers", json={"id": "main", "name": "重复"})
    assert dup.status_code == 409

    # Heartbeat data shows up in GET /api/gmod/servers
    fake_gmod_env["heartbeats"]["main"] = GModHeartbeat(
        server_id="main",
        last_seen=1_760_000_000.0,
        online=True,
        remote_ip="172.18.0.4",
        protocol_version="1",
        handlers=("echo", "ping"),
        meta={"map": "gm_construct", "players": 6, "max_players": 32},
    )
    listed = (await client.get("/api/gmod/servers")).json()
    assert len(listed["servers"]) == 1
    srv = listed["servers"][0]
    assert srv["online"] is True
    assert srv["remote_ip"] == "172.18.0.4"
    assert srv["handlers"] == ["echo", "ping"]
    assert srv["meta"]["map"] == "gm_construct"

    # Update metadata without rotating token
    upd = await client.put(
        "/api/gmod/servers/main",
        json={"name": "主服 Sandbox v2", "enabled": True, "note": "已更新", "rotate_token": False},
    )
    assert upd.status_code == 200, upd.text
    assert upd.json()["plaintext_token"] is None
    assert upd.json()["server"]["name"] == "主服 Sandbox v2"

    # Rotate token returns new plaintext_token once
    rot = await client.put(
        "/api/gmod/servers/main",
        json={"name": "主服 Sandbox v2", "enabled": True, "note": "轮换", "rotate_token": True},
    )
    assert rot.status_code == 200, rot.text
    token2 = rot.json()["plaintext_token"]
    assert isinstance(token2, str)
    assert token2.startswith("ygg_gmod_")
    assert token2 != token1
    assert decrypt(fake_gmod_env["settings"][GMOD_SERVERS_KEY].value["servers"][0]["token_enc"].encode()) == token2

    # Delete server
    deleted = await client.delete("/api/gmod/servers/main")
    assert deleted.status_code == 200
    assert (await client.delete("/api/gmod/servers/main")).status_code == 404
    assert len(published) >= 4


async def test_gmod_ping_endpoint(
    client: httpx.AsyncClient, fake_gmod_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    await client.post("/api/gmod/servers", json={"id": "main", "name": "主服", "enabled": True})
    await client.post("/api/gmod/servers", json={"id": "disabled", "name": "停用服", "enabled": False})

    # Unknown server -> 404
    assert (await client.post("/api/gmod/servers/nope/ping")).status_code == 404

    # Disabled server -> ok=False without RPC
    dis = await client.post("/api/gmod/servers/disabled/ping")
    assert dis.status_code == 200
    assert dis.json()["ok"] is False
    assert "停用" in dis.json()["error"]

    # Offline error mapped cleanly
    async def _call_offline(*args: Any, **kwargs: Any) -> GModCallResult:
        raise GModServerOffline("GMod 服务器「main」当前离线")

    monkeypatch.setattr(gmod_api, "call", _call_offline)
    off_resp = await client.post("/api/gmod/servers/main/ping")
    assert off_resp.status_code == 200
    assert off_resp.json()["ok"] is False
    assert "离线" in off_resp.json()["error"]

    # Online ping success
    async def _call_ok(server_id: str, action: str, args: dict[str, Any] | None = None, **kw: Any) -> GModCallResult:
        assert server_id == "main"
        assert action == "ping"
        return GModCallResult(
            job_id="abc123",
            server_id="main",
            action="ping",
            ok=True,
            data={"pong": True, "map": "gm_construct"},
            latency_ms=14,
        )

    monkeypatch.setattr(gmod_api, "call", _call_ok)
    ok_resp = await client.post("/api/gmod/servers/main/ping")
    assert ok_resp.status_code == 200
    assert ok_resp.json() == {
        "ok": True,
        "latency_ms": 14,
        "data": {"pong": True, "map": "gm_construct"},
        "error": None,
    }
