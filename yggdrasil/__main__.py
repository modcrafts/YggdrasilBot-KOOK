"""Bot process: ``python -m yggdrasil`` (pure FastAPI + uvicorn + self-contained KOOK gateway)."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from yggdrasil.config import APP_CONFIG, DB_CONFIG
from yggdrasil.core.log import LOGGER, setup_logging

_tasks: list[asyncio.Task] = []
_gauges: dict[str, list[tuple[float, dict[str, str]]]] = {}


async def _gauge_loop() -> None:
    """Refresh DB-derived gauges (memory lag, partitions runway, pending reviews) and sandbox workspace gauges."""
    from yggdrasil.db.partitions import months_of_runway
    from yggdrasil.db.session import engine
    from yggdrasil.moderation.review import refresh_pending
    from yggdrasil.runtime import snapshot

    while True:
        try:
            pending = await refresh_pending()
            _gauges["review"] = [(pending, {})]
            async with engine().connect() as conn:
                _gauges["runway"] = [(await months_of_runway(conn), {})]
                lag = []
                snap = snapshot()
                for cid, ch in snap.channels.items():
                    if ch.is_category or not snap.channel_enabled(cid):
                        continue
                    for pipeline in ("chunk", "topic"):
                        row = await conn.execute(
                            text(
                                "SELECT count(*) FROM messages WHERE channel_id = :c AND NOT recalled AND id > "
                                "COALESCE((SELECT last_msg_id FROM pipeline_state WHERE pipeline = :p "
                                "AND scope_id = :c), 0)"
                            ),
                            {"c": cid, "p": pipeline},
                        )
                        lag.append((float(row.scalar() or 0), {"channel": cid, "pipeline": pipeline}))
                _gauges["lag"] = lag
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("gauge refresh failed")
        await _sandbox_gauges()
        await asyncio.sleep(60)


async def _sandbox_gauges() -> None:
    from yggdrasil.sandbox import manager

    mgr = manager()
    if not mgr.configured:
        return
    try:
        await mgr.reconcile()
        infos = [i for i in await mgr.list() if not i.pending]
        _gauges["sandbox_workspaces"] = [(float(len(infos)), {})]
        _gauges["sandbox_links"] = [(float(sum(len(i.links) for i in infos)), {})]
    except asyncio.CancelledError:
        raise
    except Exception:
        LOGGER.exception("sandbox gauge refresh failed")


def _register_gauges() -> None:
    from yggdrasil.bot import state
    from yggdrasil.core.metrics import observe_gauge
    from yggdrasil.kook import gateway as kook_gw

    def kook_connected() -> list[tuple[float, dict[str, str]]]:
        st = kook_gw.status()
        return [(1.0 if (st and st.connected) else 0.0, {"protocol": "kook"})]

    def heartbeat_age() -> list[tuple[float, dict[str, str]]]:
        now = time.time()
        return [(now - ts, {"self_id": sid}) for sid, ts in state.last_heartbeat.items()]

    observe_gauge("ygg.kook.connected", "{bot}", kook_connected)
    observe_gauge("ygg.kook.heartbeat_age", "s", heartbeat_age)
    observe_gauge("ygg.review.pending", "{review}", lambda: _gauges.get("review", []))
    observe_gauge("ygg.partitions.runway", "mo", lambda: _gauges.get("runway", []))
    observe_gauge("ygg.memory.lag", "{message}", lambda: _gauges.get("lag", []))
    observe_gauge("ygg.sandbox.workspaces", "{workspace}", lambda: _gauges.get("sandbox_workspaces", []))
    observe_gauge("ygg.sandbox.links", "{link}", lambda: _gauges.get("sandbox_links", []))


def _health_routes(app: FastAPI) -> None:
    from yggdrasil.db.session import engine
    from yggdrasil.kook import client as kook_client
    from yggdrasil.kook import gateway as kook_gw

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        try:
            async with engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as e:
            return JSONResponse({"status": "db unavailable", "error": str(e)[:200]}, status_code=503)
        kook = kook_client.connected()
        return JSONResponse({"status": "ok", "bots": [kook.self_id] if kook else []})

    @app.get("/drainz", include_in_schema=False)
    async def drainz(max_wait: float = 170) -> dict[str, int]:
        """preStop hook: hold the pod until in-flight replies finish, then release the KOOK gateway lease."""
        from yggdrasil.bot.inflight import drain

        left = await drain(min(max_wait, 600))
        if (gw := kook_gw.gateway()) is not None:
            await gw.stop()
        LOGGER.info("drained for shutdown; {} tasks still running", left)
        return {"left": left}


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from pydantic_ai import Agent
    from pydantic_ai.models.instrumented import InstrumentationSettings

    from yggdrasil import runtime
    from yggdrasil.bot import router as bot_router
    from yggdrasil.core.telemetry import shutdown_telemetry
    from yggdrasil.db.session import dispose_engine, init_engine
    from yggdrasil.gmod.server import start_gmod_bridge, stop_gmod_bridge
    from yggdrasil.ingest.writer import WRITER
    from yggdrasil.kook import gateway as kook_gw
    from yggdrasil.moderation.review import expire_loop
    from yggdrasil.readiness import wait_for_dependencies
    from yggdrasil.relay.server import start_relay, stop_relay
    from yggdrasil.sandbox import manager
    from yggdrasil.sandbox.jobs import runner
    from yggdrasil.seed import seed_defaults
    from yggdrasil.stickers.drop import deliver_forever

    init_engine(statement_timeout_ms=DB_CONFIG.bot_statement_timeout_ms)
    await wait_for_dependencies()
    await seed_defaults()
    snap = await runtime.load()
    Agent.instrument_all(
        InstrumentationSettings(
            include_content=snap.settings().privacy.capture_llm_content, include_binary_content=False
        )
    )
    gw = kook_gw.install(
        on_event=bot_router.handle_event,
        on_connected=bot_router.on_connected,
        on_disconnected=bot_router.on_disconnected,
    )
    runtime.on_reload(lambda _s: gw.trigger_reload())

    _tasks.extend(
        asyncio.create_task(coro, name=name)
        for name, coro in (
            ("config-listener", runtime.listen_forever()),
            ("ingest-writer", WRITER.run()),
            ("review-expiry", expire_loop()),
            ("gauges", _gauge_loop()),
            ("sticker-drop", deliver_forever()),
        )
    )
    _tasks.append(gw.start())
    if manager().configured:
        _tasks.append(asyncio.create_task(runner().run_forever(), name="sandbox-jobs"))
    await start_relay()
    await start_gmod_bridge()
    LOGGER.info("yggdrasil KOOK bot started on {}:{}", APP_CONFIG.host, APP_CONFIG.port)

    try:
        yield
    finally:
        await stop_gmod_bridge()
        await stop_relay()
        await gw.stop()
        for task in _tasks:
            task.cancel()
        for task in _tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        _tasks.clear()
        await runner().aclose()
        with contextlib.suppress(Exception):
            await WRITER.flush()
        await dispose_engine()
        shutdown_telemetry()


def create_app() -> FastAPI:
    from yggdrasil.core.telemetry import setup_telemetry

    app = FastAPI(title="Yggdrasil KOOK Bot", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    setup_telemetry("yggdrasil-bot", app)
    _register_gauges()
    _health_routes(app)
    try:
        from yggdrasil.admin import mount
    except ImportError:
        LOGGER.warning("admin dashboard module not available")
    else:
        mount(app)
    return app


def main() -> None:
    setup_logging()
    app = create_app()
    uvicorn.run(app, host=APP_CONFIG.host, port=APP_CONFIG.port, log_config=None)


if __name__ == "__main__":
    main()
