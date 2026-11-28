"""Transcripts of real agent runs: what the console shows for tool images, retries, failures, cancellations, and with
content capture off."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from pydantic_ai import Agent, BinaryContent, ModelRetry, RunContext, ToolReturn
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from yggdrasil.transcript import store as transcript_store
from yggdrasil.transcript.capture import TranscriptRecorder, entries, live_stats
from yggdrasil.transcript.notes import Blob, TraceNotes, blob_id
from yggdrasil.transcript.schema import ChatContextDisplay, ChatMessage, Entry, ImagePart
from yggdrasil.transcript.schema import ToolCallPart as TcPart

PNG = b"\x89PNG\r\n\x1a\n-fake-image"


def _returned(messages: list[ModelMessage]) -> list[str]:
    return [p.tool_name for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


def _agent(
    respond: Any, recorder: TranscriptRecorder, slow_started: asyncio.Event | None = None
) -> Agent[TraceNotes, str]:
    agent: Agent[TraceNotes, str] = Agent(
        FunctionModel(respond), deps_type=TraceNotes, instructions="你是群聊助手。", capabilities=[recorder]
    )

    @agent.tool
    async def view_image(ctx: RunContext[TraceNotes], id: int) -> ToolReturn:
        ctx.deps.image(PNG, "image/png", segment={"ref": "abc.png", "url": "https://example.com/abc.png", "sha1": ""})
        return ToolReturn(return_value=f"图片 #{id}", content=[BinaryContent(PNG, media_type="image/png")])

    @agent.tool
    async def lookup(ctx: RunContext[TraceNotes], q: str) -> str:
        if q == "bad":
            raise ModelRetry("q 不能是 bad")
        return f"结果：{q}"

    @agent.tool
    async def slow(ctx: RunContext[TraceNotes]) -> str:
        if slow_started is not None:
            slow_started.set()
        await asyncio.sleep(30)
        return "never"

    return agent


def _kinds(items: list[Entry]) -> list[str]:
    return [e.kind if e.kind != "tool" else f"tool:{e.name}:{e.outcome}" for e in items]


async def test_a_run_reads_as_a_conversation() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        answered = sum(isinstance(m, ModelResponse) for m in messages)
        if answered == 0:
            return ModelResponse(parts=[ToolCallPart("view_image", {"id": 7}, tool_call_id="c1")])
        if answered == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "bad"}, tool_call_id="c2")])
        if answered == 2:
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "鹈鹕"}, tool_call_id="c3")])
        return ModelResponse(parts=[TextPart("鹈鹕是一种鸟。")])

    recorder = TranscriptRecorder()
    notes = TraceNotes()
    trigger = ChatMessage(user_id="10001", segments=[{"type": "text", "data": {"text": "这是什么"}}])
    notes.context = ChatContextDisplay(trigger=trigger)
    result = await _agent(respond, recorder).run("看看 #7 是什么", deps=notes)
    assert result.output == "鹈鹕是一种鸟。"

    items = entries(recorder, notes)
    assert _kinds(items) == [
        "system",
        "user",
        "assistant",
        "tool:view_image:ok",
        "assistant",
        "tool:lookup:retry",
        "assistant",
        "tool:lookup:ok",
        "assistant",
    ]
    assert items[0].parts[0].text == "你是群聊助手。"  # pyright: ignore[reportAttributeAccessIssue]
    # The trigger's QQ context sits on the first user turn only.
    assert items[1].display == notes.context
    call = items[2].parts[0]
    assert isinstance(call, TcPart)
    assert (call.id, call.args) == ("c1", {"id": 7})
    # The image view_image handed the model shows on its tool entry (not as a separate user turn), kept as a blob
    # that also stands in for the QQ image segment it came from.
    image = items[3].parts[1]
    assert isinstance(image, ImagePart)
    assert image.blob == blob_id(PNG)
    assert notes.blobs[blob_id(PNG)].data == PNG
    assert notes.image_keys == {"https://example.com/abc.png": blob_id(PNG)}
    assert "q 不能是 bad" in (items[5].parts[0].text or "")  # pyright: ignore[reportAttributeAccessIssue]
    assert items[-1].model == "function:respond:"  # the name the model reported
    assert all(e.duration_ms is not None for e in items if e.kind == "assistant")


async def test_a_failed_run_keeps_what_happened_before_the_failure() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returned(messages):
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "鹈鹕"}, tool_call_id="c1")])
        raise RuntimeError("provider exploded")

    recorder = TranscriptRecorder()
    notes = TraceNotes()
    with pytest.raises(RuntimeError):
        await _agent(respond, recorder).run("问个问题", deps=notes)

    items = entries(recorder, notes)
    assert _kinds(items) == ["system", "user", "assistant", "tool:lookup:ok", "event"]
    assert items[3].parts[0].text == "结果：鹈鹕"  # pyright: ignore[reportAttributeAccessIssue]
    assert items[-1].level == "error"
    assert "provider exploded" in (items[-1].text or "")


async def test_a_cancelled_run_marks_the_running_tool_cancelled() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("slow", {}, tool_call_id="c1")])

    recorder = TranscriptRecorder()
    notes = TraceNotes()
    started = asyncio.Event()
    task = asyncio.create_task(_agent(respond, recorder, started).run("跑个慢的", deps=notes))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    items = entries(recorder, notes)
    assert _kinds(items) == ["system", "user", "assistant", "tool:slow:cancelled"]
    assert items[-1].parts == []


async def test_metadata_mode_keeps_shapes_without_content() -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not _returned(messages):
            return ModelResponse(parts=[ToolCallPart("view_image", {"id": 7}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart("一只鸟")])

    recorder = TranscriptRecorder()
    notes = TraceNotes(full=False)
    # The pipeline sets the prompt's QQ context whatever the mode; none of it may be kept.
    notes.context = ChatContextDisplay(
        trigger=ChatMessage(user_id="10001", segments=[{"type": "text", "data": {"text": "群聊原文"}}])
    )
    await _agent(respond, recorder).run("看看 #7", deps=notes)

    items = entries(recorder, notes)
    assert _kinds(items) == ["system", "user", "assistant", "tool:view_image:ok", "assistant"]
    dumped = json.dumps([e.model_dump(mode="json") for e in items], ensure_ascii=False)
    for secret in ("你是群聊助手", "看看 #7", "图片 #7", "一只鸟", '"id": 7', "群聊原文"):
        assert secret not in dumped
    # Sizes, roles and tool names survive; no image bytes are kept.
    assert items[1].parts[0].chars == len("看看 #7")  # pyright: ignore[reportAttributeAccessIssue]
    call = items[2].parts[0]
    assert isinstance(call, TcPart)
    assert (call.name, call.args) == ("view_image", None)
    assert call.chars > 0
    assert notes.blobs == {}
    assert notes.image_keys == {}


async def test_live_progress_omits_in_flight_tool_and_flusher_coalesces(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("slow", {}, tool_call_id="c1")])

    updates = 0

    def on_update() -> None:
        nonlocal updates
        updates += 1

    recorder = TranscriptRecorder(on_update=on_update)
    notes = TraceNotes()
    started = asyncio.Event()
    task = asyncio.create_task(_agent(respond, recorder, started).run("跑个慢的", deps=notes))
    await started.wait()

    # Mid-run live snapshot omits the still-running tool entry so the UI renders its ToolCallPart as "运行中"
    live_items = entries(recorder, notes, live=True)
    assert _kinds(live_items) == ["system", "user", "assistant"]
    assert updates >= 3  # model start, model response, tool start
    models, _tin, _tout, _tcached, tool_calls = live_stats(recorder)
    assert models == ["function:respond:"]
    assert tool_calls == {"slow": 1}

    # Flusher coalesces triggers and finishes cleanly on close()
    saved: list[bool] = []
    progressed: list[dict[str, int]] = []

    async def fake_save(_inv_id: Any, _rec: Any, _notes: Any, _ch: Any, _g: Any, *, live: bool = False) -> None:
        saved.append(live)

    async def fake_progress(_inv_id: Any, rec: TranscriptRecorder) -> None:
        progressed.append(live_stats(rec)[4])

    monkeypatch.setattr(transcript_store, "save_invocation", fake_save)
    monkeypatch.setattr(transcript_store, "update_invocation_progress", fake_progress)

    inv_id = uuid4()
    flusher = transcript_store.LiveTranscriptFlusher(lambda: inv_id, recorder, notes, "ch-1", "g-1", debounce_s=0.01)
    flusher.trigger()
    flusher.trigger()
    await asyncio.sleep(0.05)
    await flusher.close()
    assert saved == [True]
    assert progressed == [{"slow": 1}]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Final non-live snapshot marks the interrupted tool cancelled
    assert _kinds(entries(recorder, notes, live=False)) == ["system", "user", "assistant", "tool:slow:cancelled"]


async def test_transcript_flushes_only_new_blobs_after_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    notes = TraceNotes()
    recorder = TranscriptRecorder()
    first_url = "https://example.org/first.png"
    second_url = "https://example.org/second.png"
    first = notes.image(PNG, "image/png", segment={"url": first_url})
    assert first is not None
    second_bytes = PNG + b"-second"
    second = blob_id(second_bytes)
    batches: list[set[str]] = []
    saved: list[dict[str, Any]] = []

    async def store_blobs(blobs: dict[str, Blob]) -> None:
        batches.append(set(blobs))
        if len(batches) == 1:
            raise RuntimeError("the blob transaction did not commit")
        if len(batches) == 2:
            # The agent can add a new image while the first blob batch is committing.
            notes.image(second_bytes, "image/png", segment={"url": second_url})

    class Session:
        async def execute(self, stmt: Any) -> None:
            saved.append(stmt.compile().params["context"])

    @asynccontextmanager
    async def session() -> AsyncIterator[Session]:
        yield Session()

    monkeypatch.setattr(transcript_store, "get_session", session)
    monkeypatch.setattr(transcript_store, "store_blobs", store_blobs)
    invocation_id = uuid4()
    await transcript_store.save_invocation(invocation_id, recorder, notes, "channel", live=True)
    assert not saved
    assert not notes.stored_blobs

    await transcript_store.save_invocation(invocation_id, recorder, notes, "channel", live=True)
    assert saved[-1]["image_keys"] == {first_url: first}
    await transcript_store.save_invocation(invocation_id, recorder, notes, "channel", live=True)
    await transcript_store.save_invocation(invocation_id, recorder, notes, "channel")

    assert batches == [{first}, {first}, {second}]
    assert len(saved) == 3
    assert saved[-1]["image_keys"] == {first_url: first, second_url: second}
