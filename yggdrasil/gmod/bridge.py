"""Redis-backed multi-server RPC transport for Garry's Mod Lua servers.

Each GMod server maintains an outbound HTTP long-polling loop against ``/bridge/gmod/poll`` (over LAN or
reverse-proxied public HTTPS) and posts execution outcomes back to ``/bridge/gmod/result``.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from redis.exceptions import TimeoutError as RedisTimeoutError

from yggdrasil.kv import redis_client
from yggdrasil.runtime import snapshot

HEARTBEAT_TTL_S = 60
_QUEUE_TTL_S = 120
_RESULT_TTL_S = 60
_MAX_BATCH = 8
MAX_POLL_WAIT_S = 25
_REDIS_BLOCK_GRACE_S = 2


class GModError(RuntimeError):
    """Base error for GMod bridge communication."""


class GModServerNotFound(GModError):
    """Server ID is not registered or disabled."""


class GModServerOffline(GModError):
    """Server has no recent heartbeat."""


class GModTimeout(GModError):
    """GMod server did not return a result within the timeout."""


class GModRemoteError(GModError):
    """GMod server executed the job and returned an application error."""


@dataclass(frozen=True, slots=True)
class GModHeartbeat:
    server_id: str
    last_seen: float
    online: bool
    remote_ip: str = ""
    protocol_version: str = ""
    handlers: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GModCallResult:
    job_id: str
    server_id: str
    action: str
    ok: bool
    data: Any = None
    error: str | None = None
    latency_ms: int = 0


def _hb_key(server_id: str) -> str:
    return f"ygg:gmod:hb:{server_id}"


def _queue_key(server_id: str) -> str:
    return f"ygg:gmod:q:{server_id}"


def _owner_key(job_id: str) -> str:
    return f"ygg:gmod:owner:{job_id}"


def _res_key(job_id: str) -> str:
    return f"ygg:gmod:res:{job_id}"


async def record_heartbeat(
    server_id: str,
    *,
    remote_ip: str = "",
    protocol_version: str = "",
    handlers: list[str] | tuple[str, ...] = (),
    meta: dict[str, Any] | None = None,
) -> GModHeartbeat:
    now = time.time()
    clean_handlers = tuple(dict.fromkeys(str(h).strip() for h in handlers if str(h).strip()))
    clean_meta = dict(meta) if isinstance(meta, dict) else {}
    payload = {
        "last_seen": now,
        "remote_ip": remote_ip,
        "protocol_version": protocol_version.strip(),
        "handlers": list(clean_handlers),
        "meta": clean_meta,
    }
    await redis_client().set(_hb_key(server_id), json.dumps(payload, ensure_ascii=False), ex=HEARTBEAT_TTL_S)
    return GModHeartbeat(
        server_id=server_id,
        last_seen=now,
        online=True,
        remote_ip=remote_ip,
        protocol_version=protocol_version.strip(),
        handlers=clean_handlers,
        meta=clean_meta,
    )


async def get_heartbeat(server_id: str) -> GModHeartbeat | None:
    raw = await redis_client().get(_hb_key(server_id))
    if not raw:
        return None
    try:
        doc = json.loads(str(raw))
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    last_seen = float(doc.get("last_seen") or 0.0)
    handlers_raw = doc.get("handlers")
    handlers = tuple(str(h) for h in handlers_raw) if isinstance(handlers_raw, list) else ()
    meta_raw = doc.get("meta")
    meta = dict(meta_raw) if isinstance(meta_raw, dict) else {}
    return GModHeartbeat(
        server_id=server_id,
        last_seen=last_seen,
        online=(time.time() - last_seen) <= HEARTBEAT_TTL_S,
        remote_ip=str(doc.get("remote_ip") or ""),
        protocol_version=str(doc.get("protocol_version") or ""),
        handlers=handlers,
        meta=meta,
    )


async def _blocking_pop(key: str, wait_s: int) -> Any:
    # Let Redis finish its own blocking wait, allowing some time for network I/O.
    async with asyncio.timeout(wait_s + _REDIS_BLOCK_GRACE_S):
        return await redis_client(blocking=True).blpop([key], timeout=wait_s)


async def pop_jobs(server_id: str, *, wait_s: float = MAX_POLL_WAIT_S) -> list[dict[str, Any]]:
    """Wait for queued jobs, drain a bounded batch, and discard expired jobs before delivery."""
    r = redis_client()
    qkey = _queue_key(server_id)
    timeout = max(0, min(round(wait_s), MAX_POLL_WAIT_S))
    first = await _blocking_pop(qkey, timeout) if timeout > 0 else await r.lpop(qkey)
    raw_items: list[Any] = []
    if timeout > 0:
        if isinstance(first, list | tuple) and len(first) == 2:
            raw_items.append(first[1])
    elif first is not None:
        raw_items.append(first)
    if not raw_items:
        return []
    for _ in range(_MAX_BATCH - 1):
        nxt = await r.lpop(qkey)
        if nxt is None:
            break
        raw_items.append(nxt)
    jobs: list[dict[str, Any]] = []
    now = time.time()
    for raw in raw_items:
        try:
            item = json.loads(str(raw))
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if "expires_at" in item:
            expires_at = item.pop("expires_at")
            if not (isinstance(expires_at, int | float) and expires_at > now):
                continue
        jobs.append(item)
    return jobs


async def submit_result(
    server_id: str,
    job_id: str,
    *,
    ok: bool,
    data: Any = None,
    error: str | None = None,
) -> bool:
    """Deliver a GMod execution result to the waiting RPC caller, verifying job ownership."""
    r = redis_client()
    okey = _owner_key(job_id)
    owner = await r.get(okey)
    if owner is None or str(owner) != server_id:
        return False
    await r.delete(okey)
    payload = json.dumps({"ok": bool(ok), "data": data, "error": error}, ensure_ascii=False)
    rkey = _res_key(job_id)
    await r.rpush(rkey, payload)
    await r.expire(rkey, _RESULT_TTL_S)
    return True


async def call(
    server_id: str,
    action: str,
    args: dict[str, Any] | None = None,
    *,
    timeout_s: float = 10.0,
    require_online: bool = True,
    check_registered: bool = True,
    raise_on_error: bool = False,
) -> GModCallResult:
    """Enqueue an RPC job for ``server_id`` and wait for the GMod server's result callback."""
    sid = server_id.strip()
    act = action.strip()
    if not sid:
        raise GModServerNotFound("server_id 不能为空")
    if not act:
        raise GModError("action 不能为空")

    if check_registered:
        info = snapshot().gmod_servers.get(sid)
        if info is None or not info.enabled:
            raise GModServerNotFound(f"找不到已启用的 GMod 服务器「{sid}」")

    if require_online:
        hb = await get_heartbeat(sid)
        if hb is None or not hb.online:
            raise GModServerOffline(f"GMod 服务器「{sid}」当前离线（无近期心跳）")

    job_id = secrets.token_hex(12)
    started = time.perf_counter()
    wait_int = max(1, round(timeout_s))
    created_at = time.time()
    job_payload = json.dumps(
        {
            "job_id": job_id,
            "action": act,
            "args": dict(args or {}),
            "created_at": created_at,
            "expires_at": created_at + wait_int,
        },
        ensure_ascii=False,
    )
    r = redis_client()
    owner_ttl = max(_QUEUE_TTL_S, int(timeout_s) + 30)
    await r.set(_owner_key(job_id), sid, ex=owner_ttl)
    qkey = _queue_key(sid)
    await r.rpush(qkey, job_payload)
    await r.expire(qkey, _QUEUE_TTL_S)

    rkey = _res_key(job_id)
    timeout_message = f"等待 GMod 服务器「{sid}」返回 {act} 回执超时（{wait_int}s），执行结果未知"
    try:
        raw = await _blocking_pop(rkey, wait_int)
    except (TimeoutError, RedisTimeoutError) as e:
        raise GModTimeout(timeout_message) from e
    latency_ms = int((time.perf_counter() - started) * 1000)
    if not (isinstance(raw, list | tuple) and len(raw) == 2):
        await r.delete(_owner_key(job_id))
        raise GModTimeout(timeout_message)

    try:
        body = json.loads(str(raw[1]))
    except ValueError as e:
        raise GModError(f"GMod 服务器「{sid}」返回了非 JSON 结果") from e
    ok = bool(body.get("ok"))
    err = str(body["error"]) if body.get("error") is not None else None
    result = GModCallResult(
        job_id=job_id,
        server_id=sid,
        action=act,
        ok=ok,
        data=body.get("data"),
        error=err,
        latency_ms=latency_ms,
    )
    if raise_on_error and not ok:
        raise GModRemoteError(err or f"GMod 服务器「{sid}」执行 {act} 失败")
    return result
