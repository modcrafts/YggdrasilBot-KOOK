"""Chat and ops deliver first and publish the final transcript before ending console polling."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import UsageLimits

from tests.support.files import png
from tests.support.kook import FakeKook, make_incoming
from yggdrasil.agent import media
from yggdrasil.agent.context import trigger_message
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.schema import BotReply
from yggdrasil.db.models import Invocation
from yggdrasil.kook import segments as seg
from yggdrasil.llm.registry import ResolvedModel
from yggdrasil.moderation.guard import Verdict
from yggdrasil.ops import agent as ops_agent
from yggdrasil.pipeline import invoke, record
from yggdrasil.runtime import Snapshot
from yggdrasil.settings import Settings
from yggdrasil.transcript import store
from yggdrasil.transcript.capture import TranscriptRecorder
from yggdrasil.transcript.notes import TraceNotes, blob_id
from yggdrasil.transcript.schema import ChatContextDisplay
from yggdrasil.triggers.index import Resolved

_URL = "https://example.org/picture.png"


@pytest.mark.parametrize(("handler", "billing_error"), [("chat", False), ("chat", True), ("ops", False)])
async def test_delivery_precedes_image_wait_and_final_transcript_precedes_status(
    handler: str, billing_error: bool, snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    incoming = make_incoming("查看附件", segments=[seg.text("查看附件"), seg.image(_URL)])
    snapshot(superusers=frozenset({incoming.user_id}))
    kook = FakeKook()
    image = png("blue")
    delivered, waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    invocation_id = uuid4()
    row = Invocation(id=invocation_id, status="running")
    writes: list[bool] = []
    billed: list[int] = []

    async def download(url: str, max_bytes: int, *, guarded: bool) -> bytes:
        assert url == _URL
        assert guarded is True
        waiting.set()
        await release.wait()
        return image

    async def save(
        _id: UUID,
        _recorder: TranscriptRecorder,
        notes: TraceNotes,
        channel_id: str | None,
        guild_id: str | None,
        *,
        live: bool = False,
    ) -> None:
        assert _id == invocation_id
        assert (channel_id, guild_id) == (incoming.channel_id, incoming.guild_id)
        assert row.status == "running"
        if not live:
            assert notes.image_keys == {_URL: blob_id(image)}
        writes.append(live)

    async def progress(_id: UUID, recorder: TranscriptRecorder) -> None:
        assert row.status == "running"

    async def finish(_id: UUID, status: str, error: str | None, meta: record.Meta, *args: Any, **kw: Any) -> None:
        assert writes[-1] is False
        assert error is None
        assert meta.models == ["test/model"]
        assert billed == [meta.tokens_in + meta.tokens_out]
        row.status = status

    async def add_tokens(channel_id: str | None, tokens: int) -> None:
        billed.append(tokens)
        if billing_error:
            raise ConnectionError("Redis unavailable")

    async def moderate(stage: str, *args: Any, **kw: Any) -> Verdict:
        return Verdict(stage, "Skipped", 0.0)

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[DeltaToolCalls]:
        output = (
            {"mode": "reply", "text": "答案"}
            if handler == "ops"
            else {
                "mode": "reply",
                "sections": [{"content": "答案"}],
            }
        )
        yield {0: DeltaToolCall(name=info.output_tools[0].name, json_args=json.dumps(output))}

    def never(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("these agents stream")

    async def deliver_chat(inv: invoke.Invocator, reply: BotReply) -> None:
        delivered.set()

    async def deliver_ops(*args: Any) -> None:
        delivered.set()

    class Session:
        def add(self, item: Invocation) -> None:
            nonlocal row
            row = item
            row.id = invocation_id

        async def flush(self) -> None:
            pass

        async def get(self, model: type[Invocation], key: UUID) -> Invocation:
            assert writes[-1] is False
            return row

    @asynccontextmanager
    async def session() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(media, "download", download)
    monkeypatch.setattr(store, "save_invocation", save)
    monkeypatch.setattr(store, "update_invocation_progress", progress)
    model = FunctionModel(never, stream_function=stream)
    resolved = ResolvedModel(handler, model, {}, UsageLimits(), "test/model")
    if handler == "chat":
        monkeypatch.setattr(invoke.record, "finish_row", finish)
        monkeypatch.setattr(invoke.ratelimit, "add_tokens", add_tokens)
        monkeypatch.setattr(invoke, "moderate", moderate)
        monkeypatch.setattr(invoke.Invocator, "deliver", deliver_chat)
        settings = Settings()
        chat = Resolved("chat", 2, "", (), "quoted_or_self", 0, False)
        inv = invoke.Invocator(kook, incoming, chat, "chat", "", settings, incoming.channel_id, incoming.guild_id)
        inv.invocation_id = invocation_id
        inv.notes.context = ChatContextDisplay(trigger=trigger_message(incoming, kook.bot_id).snapshot())
        deps = Deps(kook, incoming.channel_id, incoming.guild_id, incoming.user_id, settings, Handles(), "chat")

        async def run(inv: invoke.Invocator) -> str:
            agent = inv._agent(resolved, tools=[], capabilities=[], instructions="")  # pyright: ignore[reportPrivateUsage]
            return await inv._execute(agent, "查看附件", deps, resolved, "查看附件")  # pyright: ignore[reportPrivateUsage]

        monkeypatch.setattr(invoke.Invocator, "_run", run)
        run_call = inv.run()
    else:
        monkeypatch.setattr(ops_agent, "chat_model", lambda *args, **kw: resolved)
        monkeypatch.setattr(ops_agent, "get_session", session)
        monkeypatch.setattr(ops_agent, "_send", deliver_ops)
        run_call = ops_agent.run_ops(kook, incoming, "查看附件")

    async with asyncio.timeout(5):
        task = asyncio.create_task(run_call)
        try:
            await waiting.wait()
            assert delivered.is_set()
            assert row.status == "running"
            assert False not in writes
        finally:
            release.set()
        assert await task == ("ok" if handler == "chat" else "reply")
    assert row.status == ("ok" if handler == "chat" else "reply")
    assert writes.count(False) == 1
    assert writes[-1] is False
