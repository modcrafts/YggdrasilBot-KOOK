"""Dedicated HTTP bridge server for GMod Lua clients (internal port, optionally reverse-proxied for remote servers).

Listens on ``YGG_GMOD_BRIDGE_PORT`` (default 8091; 0 disables) without touching shared uvicorn loggers.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from yggdrasil.config import APP_CONFIG
from yggdrasil.core.log import LOGGER
from yggdrasil.gmod.bridge import MAX_POLL_WAIT_S, pop_jobs, record_heartbeat, submit_result
from yggdrasil.runtime import GModServerInfo, snapshot

_START_TIMEOUT_S = 5.0


class PollIn(BaseModel):
    server_id: str = Field(min_length=1, max_length=64)
    wait_s: int = Field(default=MAX_POLL_WAIT_S, ge=0, le=MAX_POLL_WAIT_S)
    protocol_version: str = Field(default="1", max_length=32)
    handlers: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class PollOut(BaseModel):
    jobs: list[dict[str, Any]]
    server_time: int


class ResultIn(BaseModel):
    server_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=64)
    ok: bool
    data: Any = None
    error: str | None = None


class ResultOut(BaseModel):
    ok: bool = True


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing Authorization header")
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "expected Authorization: Bearer <token>")
    return token.strip()


def _authenticate(server_id: str, authorization: str | None) -> GModServerInfo:
    token = _extract_bearer(authorization)
    info = snapshot().gmod_servers.get(server_id.strip())
    if info is None or not info.enabled or not info.token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown or disabled server")
    if not hmac.compare_digest(token, info.token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
    return info


def _remote_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def build_gmod_app() -> FastAPI:
    """Standalone ASGI app for ``/bridge/gmod/*`` (unit-testable via ASGITransport)."""
    app = FastAPI(title="Yggdrasil GMod Bridge", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/bridge/gmod/poll")
    async def poll(
        body: PollIn,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> PollOut:
        info = _authenticate(body.server_id, authorization)
        await record_heartbeat(
            info.id,
            remote_ip=_remote_ip(request),
            protocol_version=body.protocol_version,
            handlers=body.handlers,
            meta=body.meta,
        )
        jobs = await pop_jobs(info.id, wait_s=float(body.wait_s))
        return PollOut(jobs=jobs, server_time=int(time.time()))

    @app.post("/bridge/gmod/result")
    async def result(
        body: ResultIn,
        authorization: str | None = Header(default=None),
    ) -> ResultOut:
        info = _authenticate(body.server_id, authorization)
        accepted = await submit_result(info.id, body.job_id, ok=body.ok, data=body.data, error=body.error)
        if not accepted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown or expired job_id")
        return ResultOut(ok=True)

    return app


class _Server(uvicorn.Server):
    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.listening = asyncio.Event()

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        # The main uvicorn server handles process signals.
        yield

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self.listening.set()


@dataclass(slots=True)
class _Running:
    server: _Server
    task: asyncio.Task[None]
    port: int


_running: _Running | None = None


def bridge_serving_port() -> int | None:
    return _running.port if _running is not None else None


async def _serve(server: _Server) -> None:
    try:
        await server.serve()
    except SystemExit:
        pass
    except Exception:
        LOGGER.exception("gmod bridge server failed")


async def start_gmod_bridge() -> None:
    """Start the dedicated GMod bridge HTTP server on ``YGG_GMOD_BRIDGE_PORT`` (skipped when 0)."""
    global _running
    port = APP_CONFIG.gmod_bridge_port
    if _running is not None or port <= 0:
        return
    config = uvicorn.Config(
        build_gmod_app(),
        host=APP_CONFIG.host,
        port=port,
        lifespan="off",
        log_config=None,
        timeout_graceful_shutdown=5,
    )
    server = _Server(config)
    task = asyncio.create_task(_serve(server), name="gmod-bridge")
    listening = asyncio.create_task(server.listening.wait())
    await asyncio.wait({listening, task}, timeout=_START_TIMEOUT_S, return_when=asyncio.FIRST_COMPLETED)
    listening.cancel()
    if not server.listening.is_set():
        server.should_exit = True
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        LOGGER.error("gmod bridge could not listen on {}:{}", APP_CONFIG.host, port)
        return
    _running = _Running(server, task, port)
    LOGGER.info("gmod bridge listening on {}:{} (/bridge/gmod/*)", APP_CONFIG.host, port)


async def stop_gmod_bridge() -> None:
    global _running
    if _running is None:
        return
    running, _running = _running, None
    running.server.should_exit = True
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await running.task
