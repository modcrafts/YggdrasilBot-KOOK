"""HTTP client for the Azuriom Ygg Bridge plugin."""

import re
import time
from ipaddress import IPv6Address
from typing import Any
from urllib.parse import urlsplit

import httpx

from yggdrasil.gmod.queue import normalize_steamid
from yggdrasil.runtime import ToolSecretInfo, snapshot

_API_PREFIX = "/api/ygg-bridge/v1"
_HOST_LABEL = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?")


class AzuriomError(RuntimeError):
    """A request to Ygg Bridge failed."""


class AzuriomConfigError(AzuriomError):
    """The configured Ygg Bridge credentials are missing or invalid."""


class AzuriomAuthError(AzuriomError):
    """Ygg Bridge rejected the configured credentials or source address."""


def normalize_steam_identity(raw: str) -> tuple[str, str, str]:
    """Accept SteamID64 or SteamID2 and return canonical SteamID2, SteamID64 and derived SteamID3."""
    try:
        steamid2, steamid64 = normalize_steamid(raw)
    except ValueError:
        raise ValueError("无效的 SteamID，请使用 17 位 SteamID64 或 STEAM_0/1:X:Y") from None
    _, parity, account_half = steamid2.split(":")
    return steamid2, steamid64, f"[U:1:{2 * int(account_half) + int(parity)}]"


def normalize_kook_user_id(raw: str) -> str:
    value = raw.strip()
    if value.startswith("(met)") and value.endswith("(met)"):
        value = value[5:-5]
    if not re.fullmatch(r"[0-9]{1,32}", value):
        raise ValueError("无效的 KOOK 用户 ID，请使用 1 至 32 位数字或 KOOK 用户提及")
    return value


def normalize_base_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname or ""
        if ":" in hostname:
            IPv6Address(hostname)
            valid_host = True
        else:
            labels = hostname.rstrip(".").encode("idna").decode("ascii")
            valid_host = len(labels) <= 253 and all(_HOST_LABEL.fullmatch(label) for label in labels.split("."))
        if (
            parts.scheme not in {"http", "https"}
            or not valid_host
            or parts.username is not None
            or "?" in url
            or "#" in url
            or re.search(r"[\s\\]", url)
        ):
            raise ValueError
        # Accessing port also validates its syntax and range.
        parts.port
        httpx.URL(url)
    except (ValueError, httpx.InvalidURL):
        raise AzuriomConfigError("网站地址无效，请填写带合法主机名的 HTTP 或 HTTPS 地址") from None
    return url.removesuffix(_API_PREFIX).rstrip("/")


def resolve_azuriom_secret(secret: ToolSecretInfo | None = None) -> tuple[str, str, dict[str, str]]:
    sec = secret if secret is not None else snapshot().tool_secrets.get("azuriom_lookup")
    if sec is None or not sec.enabled:
        raise AzuriomConfigError("未启用独立凭据，请在「工具与技能」中配置 Azuriom 绑定查询")
    if not sec.base_url.strip():
        raise AzuriomConfigError("缺少网站地址，请填写 Azuriom 网站根地址")
    api_key = (sec.api_key or "").strip()
    if not api_key:
        raise AzuriomConfigError("缺少 API Token，请填写 Ygg Bridge 插件生成的 Bearer Token")
    return normalize_base_url(sec.base_url), api_key, dict(sec.headers)


def _check_response(response: httpx.Response, payload: Any) -> dict[str, Any]:
    status = response.status_code
    code = payload.get("error") if isinstance(payload, dict) else None
    if status == 401:
        raise AzuriomAuthError("Ygg Bridge 鉴权失败，请核对 API Token（401）")
    if status == 403:
        if code == "plugin_disabled":
            raise AzuriomAuthError("Ygg Bridge 插件未启用（403）")
        if code == "forbidden_ip":
            raise AzuriomAuthError("Ygg Bridge 拒绝当前来源 IP，请检查插件的 IP 白名单（403）")
        raise AzuriomAuthError("Ygg Bridge 拒绝访问（403）")
    if status == 422:
        if code == "invalid_kook_user_id":
            raise AzuriomError("Ygg Bridge 判定 KOOK 用户 ID 无效（422）")
        if code == "invalid_steam_id":
            raise AzuriomError("Ygg Bridge 判定 SteamID 无效（422）")
        raise AzuriomError("Ygg Bridge 判定查询参数无效（422）")
    if status != 200:
        raise AzuriomError(f"Ygg Bridge 返回异常 HTTP 状态（{status}）")
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise AzuriomError("Ygg Bridge 返回了无效的 JSON 响应")
    return payload


async def _get(path: str, *, secret: ToolSecretInfo | None = None, timeout_s: float) -> tuple[dict[str, Any], str, int]:
    base_url, api_key, custom_headers = resolve_azuriom_secret(secret)
    endpoint = f"{base_url}{_API_PREFIX}{path}"
    started = time.perf_counter()
    try:
        headers = httpx.Headers(custom_headers)
        headers["Accept"] = "application/json"
        headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            response = await client.get(endpoint, headers=headers)
    except httpx.TimeoutException:
        raise AzuriomError("Ygg Bridge 请求超时") from None
    except (httpx.RequestError, httpx.InvalidURL):
        raise AzuriomError("无法连接 Ygg Bridge，请检查网站地址和网络") from None
    except (UnicodeError, ValueError):
        raise AzuriomConfigError("Ygg Bridge 请求头配置无效") from None
    latency_ms = int((time.perf_counter() - started) * 1000)
    try:
        payload: Any = response.json()
    except ValueError:
        payload = None
    return _check_response(response, payload), endpoint, latency_ms


async def health_check(*, secret: ToolSecretInfo | None = None, timeout_s: float = 10.0) -> dict[str, Any]:
    payload, endpoint, latency_ms = await _get("/health", secret=secret, timeout_s=timeout_s)
    return {**payload, "endpoint": endpoint, "latency_ms": latency_ms}


async def lookup_by_kook(kook_user_id: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    target = normalize_kook_user_id(kook_user_id)
    payload, _, _ = await _get(f"/identity/by-kook/{target}", timeout_s=timeout_s)
    return payload


async def lookup_by_steam(steamid: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    _, steamid64, _ = normalize_steam_identity(steamid)
    payload, _, _ = await _get(f"/identity/by-steam/{steamid64}", timeout_s=timeout_s)
    return payload
