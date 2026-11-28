"""Bounded retention of KOOK request and quote image bytes."""

import asyncio
from typing import Any

import httpx
import pytest

from tests.support.files import png
from yggdrasil.agent import media
from yggdrasil.transcript.notes import TraceNotes, blob_id
from yggdrasil.transcript.schema import ChatContextDisplay, ChatMessage

_CHANNEL = "channel"
_ROOT = "https://example.org"


def _message(*names: str, channel_id: str | None = _CHANNEL, **fields: Any) -> ChatMessage:
    return ChatMessage(
        user_id="user",
        channel_id=channel_id,
        segments=[{"type": "image", "data": {"url": f"{_ROOT}/{name}"}} for name in names],
        **fields,
    )


async def test_context_retention_is_guarded_and_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    images = {"trigger": png("blue"), "quote": png("red"), "chain": png("green")}
    requested: list[str] = []
    clients = 0

    def respond(req: httpx.Request) -> httpx.Response:
        name = req.url.path.removeprefix("/")
        requested.append(name)
        if name == "expired":
            return httpx.Response(404)
        if name == "invalid":
            return httpx.Response(200, content=b"<html>an error page</html>")
        return httpx.Response(200, content=images[name])

    def guarded_client(*, timeout: float) -> httpx.AsyncClient:
        nonlocal clients
        clients += 1
        return httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=timeout)

    monkeypatch.setattr(media, "guarded_client", guarded_client)
    trigger = _message("trigger", "loaded", "expired", "invalid")
    trigger.segments += [
        {"type": "image", "data": {"blob": "already-kept", "url": f"{_ROOT}/bot"}},
        {"type": "file", "data": {"url": f"{_ROOT}/file.png"}},
    ]
    notes = TraceNotes(
        context=ChatContextDisplay(
            trigger=trigger,
            quoted=_message("quote", "trigger"),
            chain=[
                _message("chain"),
                _message("foreign", channel_id="private-other-channel"),
                _message("missing-channel", channel_id=None),
                _message("recalled", recalled=True),
            ],
            recent=[1234],
        )
    )
    notes.image(png("orange"), "image/png", segment={"url": f"{_ROOT}/loaded"})
    await media.keep_context_images(notes, _CHANNEL)

    assert clients == 5
    assert sorted(requested) == ["chain", "expired", "invalid", "quote", "trigger"]
    assert notes.image_keys == {
        f"{_ROOT}/loaded": blob_id(png("orange")),
        **{f"{_ROOT}/{name}": blob_id(data) for name, data in images.items()},
    }
    assert {blob.mime for blob in notes.blobs.values()} == {"image/png"}


async def test_metadata_capture_never_downloads_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected(*args: object, **kwargs: object) -> bytes:
        pytest.fail("metadata-only capture must not download images")

    monkeypatch.setattr(media, "download", unexpected)
    notes = TraceNotes(full=False, context=ChatContextDisplay(trigger=_message("trigger")))
    await media.keep_context_images(notes, _CHANNEL)

    assert not notes.blobs
    assert not notes.image_keys


async def test_retention_caps_attempts_and_bytes_and_keeps_dm_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    data = png("blue")

    async def download(url: str, max_bytes: int, *, guarded: bool) -> bytes:
        assert guarded is True
        assert max_bytes == 3 * 1024 * 1024
        requested.append(url)
        return data

    monkeypatch.setattr(media, "download", download)
    names = [f"picture-{i}" for i in range(14)]
    notes = TraceNotes(
        context=ChatContextDisplay(
            trigger=_message(*names, channel_id=None),
            quoted=_message("not-from-dm"),
        )
    )
    await media.keep_context_images(notes, None)

    assert requested == [f"{_ROOT}/{name}" for name in names[:12]]
    assert len(notes.image_keys) == 12
    assert list(notes.blobs) == [blob_id(data)]


async def test_retention_timeout_cancels_stalled_download(monkeypatch: pytest.MonkeyPatch) -> None:
    cancelled = asyncio.Event()
    timer = asyncio.timeout(None)
    fast_image = png("blue")

    def timeout(delay: float) -> asyncio.Timeout:
        assert delay == 5
        return timer

    async def download(url: str, max_bytes: int, *, guarded: bool) -> bytes:
        if url.endswith("/fast"):
            return fast_image
        timer.reschedule(asyncio.get_running_loop().time())
        try:
            await asyncio.Future[None]()
        finally:
            cancelled.set()
        raise AssertionError("the stalled download must be cancelled")

    monkeypatch.setattr(media.asyncio, "timeout", timeout)
    monkeypatch.setattr(media, "download", download)
    notes = TraceNotes(context=ChatContextDisplay(trigger=_message("fast", "slow")))
    await media.keep_context_images(notes, _CHANNEL)

    assert cancelled.is_set()
    assert notes.image_keys == {f"{_ROOT}/fast": blob_id(fast_image)}
