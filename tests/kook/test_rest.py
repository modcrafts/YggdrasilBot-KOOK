"""Tests for ``KookRest`` API v3 client (auth, errors, 429 retry, pagination, quote payload, asset upload)."""

import json
from typing import Any

import httpx
import pytest

from yggdrasil.kook.model import KookAuthError, KookError
from yggdrasil.kook.rest import KookRest


def _rest(handler: Any) -> KookRest:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return KookRest("test-bot-token", client=client)


async def test_auth_header_and_message_create_with_quote():
    seen: list[tuple[str, str, dict[str, Any], str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path, json.loads(req.content), req.headers.get("Authorization", "")))
        return httpx.Response(200, json={"code": 0, "message": "ok", "data": {"msg_id": "uuid-123"}})

    rest = _rest(handler)
    msg_id = await rest.message_create(target_id="ch-1", content="hi", msg_type=9, quote="orig-uuid")
    assert msg_id == "uuid-123"
    assert seen == [
        (
            "POST",
            "/api/v3/message/create",
            {"type": 9, "target_id": "ch-1", "content": "hi", "quote": "orig-uuid"},
            "Bot test-bot-token",
        )
    ]

    dm_id = await rest.direct_message_create(target_id="u-1", content="dm", quote="dm-orig", chat_code="cc-1")
    assert dm_id == "uuid-123"
    assert seen[1][1] == "/api/v3/direct-message/create"
    assert seen[1][2] == {"type": 9, "content": "dm", "chat_code": "cc-1", "quote": "dm-orig"}


async def test_auth_and_business_errors():
    rest_401 = _rest(lambda _req: httpx.Response(401, text="unauthorized"))
    with pytest.raises(KookAuthError):
        await rest_401.user_me()

    rest_code = _rest(lambda _req: httpx.Response(200, json={"code": 40100, "message": "bad token"}))
    with pytest.raises(KookAuthError):
        await rest_code.user_me()

    rest_err = _rest(lambda _req: httpx.Response(200, json={"code": 40000, "message": "bad param"}))
    with pytest.raises(KookError, match="bad param"):
        await rest_err.user_me()


async def test_rate_limit_429_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)

    monkeypatch.setattr("yggdrasil.kook.rest.asyncio.sleep", fake_sleep)

    attempts = 0

    def handler(_req: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"X-Rate-Limit-Reset": "1.5"}, text="slow down")
        return httpx.Response(200, json={"code": 0, "data": {"id": "bot-1", "username": "Ygg"}})

    rest = _rest(handler)
    me = await rest.user_me()
    assert me["id"] == "bot-1"
    assert attempts == 2
    assert sleeps == [1.5]


async def test_pagination_merges_pages():
    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(
                200,
                json={"code": 0, "data": {"items": [{"id": "g1"}, {"id": "g2"}], "meta": {"page": 1, "page_total": 2}}},
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"items": [{"id": "g3"}], "meta": {"page": 2, "page_total": 2}}},
        )

    rest = _rest(handler)
    guilds = await rest.guild_list()
    assert [g["id"] for g in guilds] == ["g1", "g2", "g3"]


async def test_asset_create_uploads_multipart():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/asset/create"
        assert b"fake-png" in req.content
        return httpx.Response(200, json={"code": 0, "data": {"url": "https://img.kookapp.cn/assets/abc.png"}})

    rest = _rest(handler)
    url = await rest.asset_create(b"fake-png", "pic.png")
    assert url == "https://img.kookapp.cn/assets/abc.png"


async def test_thread_and_extended_rest_endpoints():
    calls: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, dict(req.url.params), body))
        if req.url.path.endswith("/category/list"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [{"id": "cat-1", "name": "攻略"}]}})
        if req.url.path.endswith("/thread/list"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [{"id": "th-1", "title": "指南"}]}})
        if req.url.path.endswith("/thread/view"):
            return httpx.Response(200, json={"code": 0, "data": {"id": "th-1", "title": "指南", "content": "[]"}})
        if req.url.path.endswith("/thread/post"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [{"id": "p-1", "content": "[]"}]}})
        if req.url.path.endswith("/guild/user-list"):
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [{"id": "u-1"}], "meta": {"page": 1, "page_total": 1}}}
            )
        if req.url.path.endswith("/message/reaction-list"):
            return httpx.Response(200, json={"code": 0, "data": [{"id": "u-1", "username": "alice"}]})
        return httpx.Response(200, json={"code": 0, "data": {"id": "ok"}})

    rest = _rest(handler)
    cats = await rest.thread_category_list("ch-forum")
    assert cats == [{"id": "cat-1", "name": "攻略"}]

    threads = await rest.thread_list("ch-forum", category_id="cat-1", sort=2, time_ms=1700000000000, page_size=10)
    assert threads == [{"id": "th-1", "title": "指南"}]
    # Verify KOOK camelCase quirk: categoryId (not category_id)
    assert calls[1][2] == {
        "channel_id": "ch-forum",
        "sort": "2",
        "page_size": "10",
        "categoryId": "cat-1",
        "time": "1700000000000",
    }

    detail = await rest.thread_view("ch-forum", "th-1")
    assert detail["id"] == "th-1"

    posts = await rest.thread_post_list("ch-forum", "th-1", post_id="p-0", order="desc", page=2, page_size=15)
    assert posts == [{"id": "p-1", "content": "[]"}]
    assert calls[3][2]["post_id"] == "p-0"
    assert calls[3][2]["order"] == "desc"

    await rest.message_update("m-1", "new content", quote="q-1")
    assert calls[4][1] == "/api/v3/message/update"
    assert calls[4][3] == {"msg_id": "m-1", "content": "new content", "quote": "q-1"}

    reactors = await rest.message_reaction_list("m-1", "👍")
    assert reactors == [{"id": "u-1", "username": "alice"}]

    members = await rest.guild_user_list("g-1", search="alice", role_id=7)
    assert members == [{"id": "u-1"}]
    assert calls[6][2]["search"] == "alice"
    assert calls[6][2]["role_id"] == "7"
