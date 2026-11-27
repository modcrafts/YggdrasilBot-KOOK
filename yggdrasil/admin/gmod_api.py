"""Admin API for managing multi-server GMod Bridge connections (``/api/gmod/*``)."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found, unprocessable
from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt, mask
from yggdrasil.db.models import SettingsDoc
from yggdrasil.db.session import get_session
from yggdrasil.gmod.bridge import GModError, call, get_heartbeat
from yggdrasil.gmod.server import bridge_serving_port
from yggdrasil.runtime import GMOD_SERVERS_KEY

router = APIRouter(prefix="/gmod", tags=["gmod"])

_SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class GModServerOut(BaseModel):
    id: str
    name: str
    enabled: bool
    note: str
    token_masked: str | None
    online: bool
    last_seen: datetime | None = None
    remote_ip: str = ""
    protocol_version: str = ""
    handlers: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class GModBridgeOverviewOut(BaseModel):
    bridge_port: int
    serving: bool
    servers: list[GModServerOut]


class GModServerCreateIn(BaseModel):
    id: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    note: str = Field(default="", max_length=200)
    # Optional custom token; leave empty to auto-generate a strong random Bearer token.
    token: str | None = None


class GModServerUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    note: str = Field(default="", max_length=200)
    token: str | None = None
    rotate_token: bool = False


class GModServerCreatedOut(BaseModel):
    server: GModServerOut
    # Returned once on creation or token rotation so the admin can copy it into GMod server.cfg.
    plaintext_token: str | None = None


class GModPingOut(BaseModel):
    ok: bool
    latency_ms: int
    data: Any = None
    error: str | None = None


def _mask_token_enc(enc_raw: Any) -> str | None:
    if not isinstance(enc_raw, str) or not enc_raw:
        return None
    try:
        return mask(decrypt(enc_raw.encode()))
    except Exception:
        return "…(无法解密)"


async def _server_out(raw: dict[str, Any]) -> GModServerOut:
    sid = str(raw.get("id") or "")
    hb = await get_heartbeat(sid)
    return GModServerOut(
        id=sid,
        name=str(raw.get("name") or sid),
        enabled=bool(raw.get("enabled", True)),
        note=str(raw.get("note") or ""),
        token_masked=_mask_token_enc(raw.get("token_enc")),
        online=bool(hb and hb.online),
        last_seen=datetime.fromtimestamp(hb.last_seen, UTC) if hb else None,
        remote_ip=hb.remote_ip if hb else "",
        protocol_version=hb.protocol_version if hb else "",
        handlers=list(hb.handlers) if hb else [],
        meta=dict(hb.meta) if hb else {},
    )


async def _load_rows() -> list[dict[str, Any]]:
    async with get_session() as s:
        row = await s.get(SettingsDoc, GMOD_SERVERS_KEY)
    if row is None or not isinstance(row.value, dict):
        return []
    return [dict(x) for x in (row.value.get("servers") or ()) if isinstance(x, dict) and x.get("id")]


def _new_token() -> str:
    return f"ygg_gmod_{secrets.token_urlsafe(24)}"


@router.get("/servers")
async def list_gmod_servers() -> GModBridgeOverviewOut:
    """List configured GMod servers along with live Redis heartbeat status and registered handlers."""
    rows = await _load_rows()
    return GModBridgeOverviewOut(
        bridge_port=APP_CONFIG.gmod_bridge_port,
        serving=bridge_serving_port() is not None,
        servers=[await _server_out(r) for r in rows],
    )


@router.post("/servers")
async def create_gmod_server(body: GModServerCreateIn, actor: Actor) -> GModServerCreatedOut:
    """Register a new GMod server and generate (or encrypt) its per-server Bearer token."""
    sid = body.id.strip()
    if not _SERVER_ID_RE.fullmatch(sid):
        raise unprocessable("服务器 ID 只能包含小写字母、数字、连字符 - 和下划线 _，且以字母或数字开头（最长 48 字符）")
    name = body.name.strip()
    if not name:
        raise unprocessable("服务器名称不能为空")

    raw_token = (body.token or "").strip() or _new_token()
    entry: dict[str, Any] = {
        "id": sid,
        "name": name,
        "enabled": body.enabled,
        "note": body.note.strip(),
        "token_enc": encrypt(raw_token).decode(),
    }

    async with change(actor, "gmod_server", "create", sid) as c:
        row = await c.session.get(SettingsDoc, GMOD_SERVERS_KEY)
        doc = dict(row.value) if row and isinstance(row.value, dict) else {}
        items = [dict(x) for x in (doc.get("servers") or ()) if isinstance(x, dict)]
        if any(x.get("id") == sid for x in items):
            raise conflict(f"GMod 服务器「{sid}」已存在")
        items.append(entry)
        doc["servers"] = items
        if row is None:
            c.session.add(SettingsDoc(key=GMOD_SERVERS_KEY, value=doc))
        else:
            row.value = doc
        c.detail = {"id": sid, "name": name, "enabled": body.enabled}

    return GModServerCreatedOut(server=await _server_out(entry), plaintext_token=raw_token)


@router.put("/servers/{server_id}")
async def update_gmod_server(server_id: str, body: GModServerUpdateIn, actor: Actor) -> GModServerCreatedOut:
    """Update a GMod server's metadata or rotate its Bearer token."""
    sid = server_id.strip()
    name = body.name.strip()
    if not name:
        raise unprocessable("服务器名称不能为空")

    plaintext: str | None = None
    updated_entry: dict[str, Any] = {}

    async with change(actor, "gmod_server", "update", sid) as c:
        row = await c.session.get(SettingsDoc, GMOD_SERVERS_KEY)
        doc = dict(row.value) if row and isinstance(row.value, dict) else {}
        items = [dict(x) for x in (doc.get("servers") or ()) if isinstance(x, dict)]
        idx = next((i for i, x in enumerate(items) if x.get("id") == sid), None)
        if idx is None:
            raise not_found(f"GMod 服务器 {sid}")
        entry = dict(items[idx])
        entry["name"] = name
        entry["enabled"] = body.enabled
        entry["note"] = body.note.strip()
        if body.rotate_token:
            plaintext = _new_token()
            entry["token_enc"] = encrypt(plaintext).decode()
        elif body.token and body.token.strip():
            plaintext = body.token.strip()
            entry["token_enc"] = encrypt(plaintext).decode()
        items[idx] = entry
        doc["servers"] = items
        if row is not None:
            row.value = doc
        updated_entry = entry
        c.detail = {"id": sid, "name": name, "enabled": body.enabled, "token_changed": plaintext is not None}

    return GModServerCreatedOut(server=await _server_out(updated_entry), plaintext_token=plaintext)


@router.delete("/servers/{server_id}")
async def delete_gmod_server(server_id: str, actor: Actor) -> Ok:
    """Delete a GMod server registration."""
    sid = server_id.strip()
    async with change(actor, "gmod_server", "delete", sid) as c:
        row = await c.session.get(SettingsDoc, GMOD_SERVERS_KEY)
        doc = dict(row.value) if row and isinstance(row.value, dict) else {}
        items = [dict(x) for x in (doc.get("servers") or ()) if isinstance(x, dict)]
        kept = [x for x in items if x.get("id") != sid]
        if len(kept) == len(items):
            raise not_found(f"GMod 服务器 {sid}")
        doc["servers"] = kept
        if row is not None:
            row.value = doc
        c.detail = {"id": sid}
    return Ok()


@router.post("/servers/{server_id}/ping")
async def ping_gmod_server(server_id: str) -> GModPingOut:
    """Run a live end-to-end ``ping`` RPC against one connected GMod server."""
    sid = server_id.strip()
    rows = await _load_rows()
    target = next((r for r in rows if r.get("id") == sid), None)
    if target is None:
        raise not_found(f"GMod 服务器 {sid}")
    if not bool(target.get("enabled", True)):
        return GModPingOut(ok=False, latency_ms=0, error="该服务器当前处于停用状态")
    try:
        res = await call(
            sid,
            "ping",
            {"source": "dashboard"},
            timeout_s=8.0,
            require_online=True,
            check_registered=False,
        )
    except GModError as e:
        return GModPingOut(ok=False, latency_ms=0, error=str(e))
    return GModPingOut(ok=res.ok, latency_ms=res.latency_ms, data=res.data, error=res.error)
