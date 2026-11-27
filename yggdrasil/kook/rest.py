"""KOOK Open Platform HTTP API v3 client (pure ``httpx``).

Docs: https://developer.kookapp.cn/doc/http/
"""

import asyncio
from typing import Any

import httpx

from yggdrasil.core.log import LOGGER
from yggdrasil.kook.model import KookAuthError, KookError, KookRateLimited

API_BASE = "https://www.kookapp.cn/api/v3"
DEFAULT_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 120.0
MAX_RATE_LIMIT_RETRIES = 3


class KookRest:
    """Thin async REST client for KOOK API v3."""

    def __init__(self, token: str, *, base_url: str = API_BASE, client: httpx.AsyncClient | None = None) -> None:
        self._token = token.strip()
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=DEFAULT_TIMEOUT_S,
            headers={"Authorization": f"Bot {self._token}"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: Any = None,
        timeout_s: float | None = None,
    ) -> Any:
        """Send an authenticated API v3 request and unwrap ``data``."""
        url = path if path.startswith("http") else f"{self._base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bot {self._token}"}
        req_timeout = timeout_s or (UPLOAD_TIMEOUT_S if files is not None else DEFAULT_TIMEOUT_S)
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                resp = await self._client.request(
                    method.upper(),
                    url,
                    params=params,
                    json=json,
                    files=files,
                    headers=headers,
                    timeout=req_timeout,
                )
            except httpx.HTTPError as e:
                raise KookError(f"{method} {path} network error: {e}") from e

            if resp.status_code == 429:
                reset = resp.headers.get("X-Rate-Limit-Reset") or resp.headers.get("Retry-After")
                wait = _parse_float(reset, default=float(2**attempt))
                wait = min(max(wait, 0.5), 30.0)
                if attempt < MAX_RATE_LIMIT_RETRIES:
                    LOGGER.warning("KOOK 429 on {} {}; retrying in {:.1f}s", method, path, wait)
                    await asyncio.sleep(wait)
                    continue
                raise KookRateLimited(f"KOOK rate limited on {method} {path}", retry_after=wait)

            if resp.status_code in (401, 403):
                raise KookAuthError(
                    f"KOOK auth error ({resp.status_code}) on {path}: {resp.text[:200]}",
                    status_code=resp.status_code,
                )

            try:
                body = resp.json()
            except ValueError as e:
                raise KookError(
                    f"KOOK non-JSON response ({resp.status_code}) on {path}: {resp.text[:200]}",
                    status_code=resp.status_code,
                ) from e

            if not isinstance(body, dict):
                raise KookError(f"KOOK unexpected response on {path}: {str(body)[:200]}", status_code=resp.status_code)

            code = body.get("code")
            if code == 0:
                return body.get("data")
            msg = str(body.get("message") or f"code={code}")
            if code in (401, 40100, 40101, 40102, 40103):
                raise KookAuthError(f"KOOK auth failed ({code}): {msg}", code=code, status_code=resp.status_code)
            raise KookError(f"KOOK API {path} failed ({code}): {msg}", code=code, status_code=resp.status_code)

        raise KookRateLimited(f"KOOK rate limited on {method} {path}")

    # --- gateway & identity -------------------------------------------------------------------------------------

    async def gateway_index(self, *, compress: bool = True) -> str:
        data = await self.request("GET", "/gateway/index", params={"compress": 1 if compress else 0})
        url = str((data or {}).get("url") or "")
        if not url:
            raise KookError("KOOK /gateway/index returned empty url")
        return url

    async def user_me(self) -> dict[str, Any]:
        return dict(await self.request("GET", "/user/me") or {})

    async def user_view(self, user_id: str, *, guild_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"user_id": user_id}
        if guild_id:
            params["guild_id"] = guild_id
        return dict(await self.request("GET", "/user/view", params=params) or {})

    # --- guilds, channels, roles, emojis ------------------------------------------------------------------------

    async def _paginate(
        self, path: str, params: dict[str, Any] | None = None, *, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        base_params = dict(params or {})
        for page in range(1, max_pages + 1):
            data = await self.request("GET", path, params={**base_params, "page": page, "page_size": 50})
            if not isinstance(data, dict):
                break
            batch = data.get("items") or []
            if isinstance(batch, list):
                items.extend(x for x in batch if isinstance(x, dict))
            meta = data.get("meta") or {}
            page_total = int(meta.get("page_total") or 1)
            if page >= page_total or not batch:
                break
        return items

    async def guild_list(self) -> list[dict[str, Any]]:
        return await self._paginate("/guild/list")

    async def guild_view(self, guild_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/guild/view", params={"guild_id": guild_id}) or {})

    async def guild_role_list(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._paginate("/guild-role/list", {"guild_id": guild_id})

    async def guild_emoji_list(self, guild_id: str) -> list[dict[str, Any]]:
        return await self._paginate("/guild-emoji/list", {"guild_id": guild_id})

    async def channel_list(self, guild_id: str, *, type: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"guild_id": guild_id}
        if type is not None:
            params["type"] = type
        return await self._paginate("/channel/list", params)

    async def channel_view(self, channel_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/channel/view", params={"target_id": channel_id}) or {})

    async def guild_user_list(
        self,
        guild_id: str,
        *,
        channel_id: str | None = None,
        search: str | None = None,
        role_id: int | None = None,
        mobile_verified: int | None = None,
        active_time: int | None = None,
        joined_at: int | None = None,
        max_pages: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"guild_id": guild_id}
        if channel_id:
            params["channel_id"] = channel_id
        if search:
            params["search"] = search
        if role_id is not None:
            params["role_id"] = role_id
        if mobile_verified is not None:
            params["mobile_verified"] = mobile_verified
        if active_time is not None:
            params["active_time"] = active_time
        if joined_at is not None:
            params["joined_at"] = joined_at
        return await self._paginate("/guild/user-list", params, max_pages=max_pages)

    async def channel_user_list(self, channel_id: str) -> list[dict[str, Any]]:
        data = await self.request("GET", "/channel/user-list", params={"channel_id": channel_id})
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            items = data.get("items") or []
            if isinstance(items, list):
                return [x for x in items if isinstance(x, dict)]
        return []

    async def channel_user_get_joined(self, guild_id: str, user_id: str) -> list[dict[str, Any]]:
        return await self._paginate(
            "/channel-user/get-joined-channel",
            {"guild_id": guild_id, "user_id": user_id},
            max_pages=5,
        )

    # --- channel messages ---------------------------------------------------------------------------------------

    async def message_create(
        self,
        *,
        target_id: str,
        content: str,
        msg_type: int = 9,
        quote: str | None = None,
        temp_target_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {"type": msg_type, "target_id": target_id, "content": content}
        if quote:
            payload["quote"] = quote
        if temp_target_id:
            payload["temp_target_id"] = temp_target_id
        data = await self.request("POST", "/message/create", json=payload, timeout_s=timeout_s)
        msg_id = str((data or {}).get("msg_id") or "")
        if not msg_id:
            raise KookError("KOOK /message/create returned empty msg_id")
        return msg_id

    async def message_view(self, msg_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/message/view", params={"msg_id": msg_id}) or {})

    async def message_list(
        self,
        target_id: str,
        *,
        msg_id: str | None = None,
        pin: int | None = None,
        flag: str = "before",
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"target_id": target_id, "page_size": min(max(page_size, 1), 100)}
        if msg_id:
            params["msg_id"] = msg_id
            params["flag"] = flag
        if pin is not None:
            params["pin"] = pin
        data = await self.request("GET", "/message/list", params=params)
        items = (data or {}).get("items") or []
        return [x for x in items if isinstance(x, dict)]

    async def message_delete(self, msg_id: str) -> None:
        await self.request("POST", "/message/delete", json={"msg_id": msg_id})

    async def message_update(
        self,
        msg_id: str,
        content: str,
        *,
        quote: str | None = None,
        temp_target_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"msg_id": msg_id, "content": content}
        if quote is not None:
            payload["quote"] = quote
        if temp_target_id:
            payload["temp_target_id"] = temp_target_id
        await self.request("POST", "/message/update", json=payload)

    async def message_reaction_list(self, msg_id: str, emoji: str) -> list[dict[str, Any]]:
        data = await self.request("GET", "/message/reaction-list", params={"msg_id": msg_id, "emoji": emoji})
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    async def message_add_reaction(self, msg_id: str, emoji: str) -> None:
        await self.request("POST", "/message/add-reaction", json={"msg_id": msg_id, "emoji": emoji})

    async def message_delete_reaction(self, msg_id: str, emoji: str, *, user_id: str | None = None) -> None:
        payload: dict[str, Any] = {"msg_id": msg_id, "emoji": emoji}
        if user_id:
            payload["user_id"] = user_id
        await self.request("POST", "/message/delete-reaction", json=payload)

    async def message_pin(self, msg_id: str, target_id: str) -> None:
        await self.request("POST", "/message/pin", json={"msg_id": msg_id, "target_id": target_id})

    async def message_unpin(self, msg_id: str, target_id: str) -> None:
        await self.request("POST", "/message/unpin", json={"msg_id": msg_id, "target_id": target_id})

    # --- direct messages ----------------------------------------------------------------------------------------

    async def direct_message_create(
        self,
        *,
        target_id: str,
        content: str,
        msg_type: int = 9,
        quote: str | None = None,
        chat_code: str | None = None,
        timeout_s: float | None = None,
    ) -> str:
        payload: dict[str, Any] = {"type": msg_type, "content": content}
        if chat_code:
            payload["chat_code"] = chat_code
        else:
            payload["target_id"] = target_id
        if quote:
            payload["quote"] = quote
        data = await self.request("POST", "/direct-message/create", json=payload, timeout_s=timeout_s)
        msg_id = str((data or {}).get("msg_id") or "")
        if not msg_id:
            raise KookError("KOOK /direct-message/create returned empty msg_id")
        return msg_id

    async def direct_message_view(self, msg_id: str, chat_code: str) -> dict[str, Any]:
        return dict(
            await self.request("GET", "/direct-message/view", params={"msg_id": msg_id, "chat_code": chat_code}) or {}
        )

    async def direct_message_delete(self, msg_id: str) -> None:
        await self.request("POST", "/direct-message/delete", json={"msg_id": msg_id})

    async def direct_message_update(self, msg_id: str, content: str, *, quote: str | None = None) -> None:
        payload: dict[str, Any] = {"msg_id": msg_id, "content": content}
        if quote is not None:
            payload["quote"] = quote
        await self.request("POST", "/direct-message/update", json=payload)

    async def direct_message_list(
        self,
        *,
        chat_code: str | None = None,
        target_id: str | None = None,
        msg_id: str | None = None,
        flag: str = "before",
        page_size: int = 50,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page_size": min(max(page_size, 1), 100)}
        if chat_code:
            params["chat_code"] = chat_code
        elif target_id:
            params["target_id"] = target_id
        if msg_id:
            params["msg_id"] = msg_id
            params["flag"] = flag
        data = await self.request("GET", "/direct-message/list", params=params)
        items = (data or {}).get("items") or []
        return [x for x in items if isinstance(x, dict)]

    async def direct_message_reaction_list(self, msg_id: str, emoji: str) -> list[dict[str, Any]]:
        data = await self.request("GET", "/direct-message/reaction-list", params={"msg_id": msg_id, "emoji": emoji})
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    async def direct_message_add_reaction(self, msg_id: str, emoji: str) -> None:
        await self.request("POST", "/direct-message/add-reaction", json={"msg_id": msg_id, "emoji": emoji})

    async def direct_message_delete_reaction(self, msg_id: str, emoji: str, *, user_id: str | None = None) -> None:
        payload: dict[str, Any] = {"msg_id": msg_id, "emoji": emoji}
        if user_id:
            payload["user_id"] = user_id
        await self.request("POST", "/direct-message/delete-reaction", json=payload)

    async def user_chat_list(self) -> list[dict[str, Any]]:
        return await self._paginate("/user-chat/list")

    async def user_chat_view(self, chat_code: str) -> dict[str, Any]:
        return dict(await self.request("GET", "/user-chat/view", params={"chat_code": chat_code}) or {})

    async def user_chat_create(self, target_id: str) -> dict[str, Any]:
        return dict(await self.request("POST", "/user-chat/create", json={"target_id": target_id}) or {})

    # --- forum / thread channels (channel type 4) ---------------------------------------------------------------

    async def thread_category_list(self, channel_id: str) -> list[dict[str, Any]]:
        """List forum categories inside a KOOK thread channel (``GET /category/list``)."""
        data = await self.request("GET", "/category/list", params={"channel_id": channel_id})
        if isinstance(data, dict):
            raw = data.get("list") or data.get("items") or []
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []

    async def thread_list(
        self,
        channel_id: str,
        *,
        category_id: str | None = None,
        sort: int = 1,
        time_ms: int | None = None,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """List threads in a forum channel (``GET /thread/list``).

        Note KOOK wire quirks:
        - The category filter query parameter is camelCase ``categoryId`` (not ``category_id``).
        - ``sort``: ``0`` = inherited default, ``1`` = latest activity, ``2`` = creation time.
        - Returned thread summaries have ``content = None``; call :meth:`thread_view` for full content.
        """
        params: dict[str, Any] = {
            "channel_id": channel_id,
            "sort": sort,
            "page_size": min(max(page_size, 1), 100),
        }
        if category_id:
            params["categoryId"] = category_id
        if time_ms is not None:
            params["time"] = time_ms
        data = await self.request("GET", "/thread/list", params=params)
        items = (data or {}).get("items") or [] if isinstance(data, dict) else []
        return [x for x in items if isinstance(x, dict)]

    async def thread_view(self, channel_id: str, thread_id: str) -> dict[str, Any]:
        """Fetch full thread details including main post CardMessage ``content`` (``GET /thread/view``)."""
        return dict(
            await self.request("GET", "/thread/view", params={"channel_id": channel_id, "thread_id": thread_id}) or {}
        )

    async def thread_post_list(
        self,
        channel_id: str,
        thread_id: str,
        *,
        post_id: str | None = None,
        time_ms: int | None = None,
        order: int = 1,
        page: int = 1,
        page_size: int = 20,
    ) -> list[dict[str, Any]]:
        """List top-level comments (``post_id=None``) or nested floor replies (``post_id=<id>``) in a thread."""
        params: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_id": thread_id,
            "order": order,
            "page": max(page, 1),
            "page_size": min(max(page_size, 1), 100),
        }
        if post_id:
            params["post_id"] = post_id
        if time_ms is not None:
            params["time"] = time_ms
        data = await self.request("GET", "/thread/post", params=params)
        items = (data or {}).get("items") or [] if isinstance(data, dict) else []
        return [x for x in items if isinstance(x, dict)]

    async def thread_create(
        self,
        channel_id: str,
        guild_id: str,
        title: str,
        content: str,
        *,
        category_id: str | None = None,
        cover: str | None = None,
        tag_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        """Create a new forum thread (``content`` must be KOOK CardMessage JSON)."""
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "guild_id": guild_id,
            "title": title,
            "content": content,
        }
        if category_id:
            payload["category_id"] = category_id
        if cover:
            payload["cover"] = cover
        if tag_ids:
            payload["tag_ids"] = tag_ids
        return dict(await self.request("POST", "/thread/create", json=payload) or {})

    async def thread_reply(
        self,
        channel_id: str,
        thread_id: str,
        content: str,
        *,
        reply_id: str | None = None,
    ) -> dict[str, Any]:
        """Reply to a thread (``reply_id=None``) or to a specific post/reply (``content`` is CardMessage JSON)."""
        payload: dict[str, Any] = {
            "channel_id": channel_id,
            "thread_id": thread_id,
            "content": content,
        }
        if reply_id:
            payload["reply_id"] = reply_id
        return dict(await self.request("POST", "/thread/reply", json=payload) or {})

    async def thread_delete(
        self,
        channel_id: str,
        *,
        thread_id: str | None = None,
        post_id: str | None = None,
    ) -> None:
        """Delete a thread (``thread_id`` only), main post content, or a reply (``post_id``)."""
        payload: dict[str, Any] = {"channel_id": channel_id}
        if thread_id:
            payload["thread_id"] = thread_id
        if post_id:
            payload["post_id"] = post_id
        await self.request("POST", "/thread/delete", json=payload)

    # --- assets -------------------------------------------------------------------------------------------------

    async def asset_create(self, data: bytes, filename: str) -> str:
        """Upload binary data to KOOK CDN; returns the permanent asset URL."""
        res = await self.request("POST", "/asset/create", files={"file": (filename, data)}, timeout_s=UPLOAD_TIMEOUT_S)
        url = str((res or {}).get("url") or "")
        if not url:
            raise KookError("KOOK /asset/create returned empty url")
        return url


def _parse_float(value: str | None, *, default: float) -> float:
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default
