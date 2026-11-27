"""Tests for OpenAI Content Provenance API client and ``check_provenance`` agent tool."""

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from tests.support.kook import FakeKook
from yggdrasil.agent.deps import Deps, Handles
from yggdrasil.agent.tools import attachments
from yggdrasil.config import APP_CONFIG
from yggdrasil.runtime import ProviderInfo, Snapshot, ToolSecretInfo
from yggdrasil.settings import Settings
from yggdrasil.tools.provenance import (
    ProvenanceError,
    check_content_provenance,
    describe_provenance_source,
    format_provenance_report,
    resolve_provenance_endpoint,
)
from yggdrasil.transcript.notes import TraceNotes

FAKE_PNG = b"\x89PNG\r\n\x1a\n-raw-original-bytes-with-c2pa"


def _provider(pid: int, name: str, base_url: str, api_key: str | None = "sk-test") -> ProviderInfo:
    return ProviderInfo(pid, name, base_url, api_key, {}, True, ("gpt-5",), "openai", {})


def test_resolve_provenance_endpoint_priority(
    snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(APP_CONFIG, "openai_api_key", "sk-env")
    snapshot(
        providers={
            1: _provider(1, "OpenRouter", "https://openrouter.ai/api/v1", "sk-or"),
            2: _provider(2, "OpenAI Official", "https://api.openai.com/v1", "sk-official"),
            3: _provider(3, "Relay", "https://relay.example.com/v1", "sk-relay"),
        }
    )
    # 1. Explicit provider_ref wins over standalone secret and auto-detect
    snapshot(
        providers={
            1: _provider(1, "OpenRouter", "https://openrouter.ai/api/v1", "sk-or"),
            2: _provider(2, "OpenAI Official", "https://api.openai.com/v1", "sk-official"),
            3: _provider(3, "Relay", "https://relay.example.com/v1", "sk-relay"),
        },
        tool_secrets={
            "check_provenance": ToolSecretInfo(
                enabled=True, base_url="https://custom.example.com/v1", api_key="sk-custom", headers={"X-Org": "acme"}
            )
        },
    )
    assert resolve_provenance_endpoint("Relay")[:2] == ("https://relay.example.com/v1", "sk-relay")
    assert resolve_provenance_endpoint("3")[:2] == ("https://relay.example.com/v1", "sk-relay")
    assert describe_provenance_source("Relay").mode == "provider"

    # 2. Enabled standalone tool secret wins over auto-detect when provider_ref is empty
    assert resolve_provenance_endpoint() == ("https://custom.example.com/v1", "sk-custom", {"X-Org": "acme"})
    assert describe_provenance_source().mode == "custom"

    # 3. Auto-detect official api.openai.com provider when standalone secret is disabled
    snapshot(
        providers={
            1: _provider(1, "OpenRouter", "https://openrouter.ai/api/v1", "sk-or"),
            2: _provider(2, "OpenAI Official", "https://api.openai.com/v1", "sk-official"),
        },
        tool_secrets={"check_provenance": ToolSecretInfo(enabled=False, api_key="sk-custom")},
    )
    assert resolve_provenance_endpoint()[:2] == ("https://api.openai.com/v1", "sk-official")
    assert describe_provenance_source().mode == "auto_provider"

    # 4. Fallback to OPENAI_API_KEY env var when no official provider is registered
    snapshot(providers={1: _provider(1, "OpenRouter", "https://openrouter.ai/api/v1", "sk-or")})
    assert resolve_provenance_endpoint()[:2] == ("https://api.openai.com/v1", "sk-env")
    assert describe_provenance_source().mode == "env"

    # 5. Clear error when nothing is configured
    monkeypatch.setattr(APP_CONFIG, "openai_api_key", "")
    assert describe_provenance_source().configured is False
    with pytest.raises(ProvenanceError, match="未配置"):
        resolve_provenance_endpoint()


async def test_check_content_provenance_parses_c2pa_and_synthid(snapshot: Callable[..., Snapshot]) -> None:
    snapshot(providers={1: _provider(1, "OpenAI", "https://api.openai.com/v1", "sk-live")})
    seen: list[tuple[str, str, bytes]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url), req.content))
        assert req.headers.get("Authorization") == "Bearer sk-live"
        return httpx.Response(
            200,
            json={
                "created_at": 1_740_000_000,
                "object": "content_provenance_check",
                "results": [
                    {
                        "type": "c2pa",
                        "outcome": "detected",
                        "validation_state": "trusted",
                        "issuer": "OpenAI OpCo, LLC",
                        "model": "gpt-image",
                        "generated_at": "2026-03-30T12:00:00Z",
                    },
                    {
                        "type": "synthid",
                        "outcome": "not_detected",
                        "model": None,
                        "generated_at": None,
                    },
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    report = await check_content_provenance(FAKE_PNG, "sample.png", mime="image/png", client=client)
    assert len(seen) == 1
    assert seen[0][0] == "POST"
    assert seen[0][1] == "https://api.openai.com/v1/content_provenance_checks"
    assert FAKE_PNG in seen[0][2]
    assert report.any_detected is True
    assert report.results[0].type == "c2pa"
    assert report.results[0].issuer == "OpenAI OpCo, LLC"
    rendered = format_provenance_report(report, filename="sample.png")
    assert "C2PA 内容凭证：已检测到" in rendered
    assert "OpenAI OpCo, LLC" in rendered
    assert "SynthID 水印：未检测到" in rendered


async def test_check_provenance_tool_uploads_raw_bytes_untouched(
    snapshot: Callable[..., Snapshot], monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot(providers={1: _provider(1, "OpenAI", "https://api.openai.com/v1", "sk-live")})
    uploaded: list[tuple[bytes, str]] = []

    async def fake_media_bytes(_h: Any, _max_bytes: int, **_kw: Any) -> tuple[bytes, str]:
        return FAKE_PNG, "orig.png"

    async def fake_check(data: bytes, filename: str, **_kw: Any) -> Any:
        uploaded.append((data, filename))
        from yggdrasil.tools.provenance import ProvenanceReport, ProvenanceSignal

        return ProvenanceReport(
            created_at=1_740_000_000,
            results=(ProvenanceSignal(type="c2pa", outcome="not_detected"),),
        )

    monkeypatch.setattr(attachments, "media_bytes", fake_media_bytes)
    monkeypatch.setattr("yggdrasil.tools.provenance.check_content_provenance", fake_check)

    handles = Handles()
    key = handles.add("image", {"url": "https://img.kookapp.cn/orig.png", "name": "orig.png"})
    assert key == "img#1"
    deps = Deps(
        kook=FakeKook(),
        channel_id="ch-1",
        guild_id="g-1",
        user_id="u-1",
        settings=Settings(),
        handles=handles,
        task="chat",
        notes=TraceNotes(),
    )
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage(), tool_call_id="tc-1")
    out = await attachments.check_provenance(ctx, "img#1")
    assert isinstance(out, str)
    assert uploaded == [(FAKE_PNG, "orig.png")]
    assert "未检测到" in out
    assert "并不等同于证明非 AI 生成" in out
