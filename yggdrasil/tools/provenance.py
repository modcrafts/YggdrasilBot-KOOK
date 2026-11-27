"""OpenAI Content Provenance API client (``POST /v1/content_provenance_checks``).

Inspects raw images, audio files, or PDFs for supported OpenAI provenance signals (C2PA Content Credentials
and SynthID watermarks). Never re-encodes input bytes so embedded C2PA manifests and watermarks remain intact.

Docs: https://developers.openai.com/api/docs/guides/content-provenance
"""

import mimetypes
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from yggdrasil.config import APP_CONFIG
from yggdrasil.runtime import ProviderInfo, ToolSecretInfo, snapshot

OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
PROVENANCE_TIMEOUT_S = 30.0
PROVENANCE_MAX_BYTES = 25 * 1024 * 1024


class ProvenanceError(RuntimeError):
    """Content provenance check failed or is not configured."""


@dataclass(frozen=True, slots=True)
class ProvenanceSourceStatus:
    """Resolved credential source for ``check_provenance`` (used by the admin dashboard & diagnostics)."""

    mode: str  # provider | custom | auto_provider | env | unconfigured | invalid_provider
    configured: bool
    label: str
    endpoint: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ProvenanceSignal:
    type: str  # c2pa | synthid | ...
    outcome: str  # detected | not_detected | ...
    validation_state: str | None = None
    issuer: str | None = None
    model: str | None = None
    generated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ProvenanceReport:
    created_at: int | None
    results: tuple[ProvenanceSignal, ...]

    @property
    def any_detected(self) -> bool:
        return any(r.outcome == "detected" for r in self.results)


def _match_provider(provider_ref: str) -> ProviderInfo | None:
    ref = provider_ref.strip()
    if not ref:
        return None
    providers = snapshot().providers
    if ref.isdigit() and (p := providers.get(int(ref))) is not None and p.enabled:
        return p
    ref_lower = ref.lower()
    for p in providers.values():
        if p.enabled and p.name.strip().lower() == ref_lower:
            return p
    return None


def _is_official_openai(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "api.openai.com"


def _active_secret(secret: ToolSecretInfo | None) -> ToolSecretInfo | None:
    if secret is not None:
        return secret
    return snapshot().tool_secrets.get("check_provenance")


def describe_provenance_source(
    provider_ref: str = "", *, secret: ToolSecretInfo | None = None
) -> ProvenanceSourceStatus:
    """Inspect which OpenAI Content Provenance credential source is active without raising."""
    if provider_ref.strip():
        p = _match_provider(provider_ref)
        if p is None:
            return ProvenanceSourceStatus(
                mode="invalid_provider",
                configured=False,
                label="提供商无效",
                detail=f"找不到已启用的提供商「{provider_ref}」，请重新选择提供商或切换连接模式",
            )
        if not p.api_key:
            return ProvenanceSourceStatus(
                mode="invalid_provider",
                configured=False,
                label=f"缺少 Key（{p.name}）",
                endpoint=_endpoint_url(p.base_url),
                detail=f"提供商「{p.name}」未配置 API Key",
            )
        return ProvenanceSourceStatus(
            mode="provider",
            configured=True,
            label=f"复用 Provider「{p.name}」",
            endpoint=_endpoint_url(p.base_url),
            detail=f"使用提供商「{p.name}」（{p.base_url}）",
        )

    sec = _active_secret(secret)
    if sec is not None and sec.enabled:
        base = sec.base_url or OPENAI_DEFAULT_BASE
        if not sec.api_key:
            return ProvenanceSourceStatus(
                mode="custom",
                configured=False,
                label="独立凭据缺少 Key",
                endpoint=_endpoint_url(base),
                detail="已启用独立凭据但尚未填写 API Key",
            )
        return ProvenanceSourceStatus(
            mode="custom",
            configured=True,
            label="独立凭据",
            endpoint=_endpoint_url(base),
            detail=f"使用工具独立凭据（{base}）",
        )

    for p in snapshot().providers.values():
        if p.enabled and p.api_format == "openai" and p.api_key and _is_official_openai(p.base_url):
            return ProvenanceSourceStatus(
                mode="auto_provider",
                configured=True,
                label=f"自动匹配「{p.name}」",
                endpoint=_endpoint_url(p.base_url),
                detail=f"自动匹配 OpenAI 官方提供商「{p.name}」",
            )

    if APP_CONFIG.openai_api_key.strip():
        return ProvenanceSourceStatus(
            mode="env",
            configured=True,
            label="环境变量 OPENAI_API_KEY",
            endpoint=_endpoint_url(OPENAI_DEFAULT_BASE),
            detail="使用部署环境变量 OPENAI_API_KEY",
        )

    return ProvenanceSourceStatus(
        mode="unconfigured",
        configured=False,
        label="未配置",
        detail="请在工具配置中选择 OpenAI 提供商、填写独立 API Key，或配置环境变量 OPENAI_API_KEY",
    )


def resolve_provenance_endpoint(
    provider_ref: str = "", *, secret: ToolSecretInfo | None = None
) -> tuple[str, str, dict[str, str]]:
    """Resolve ``(base_url, api_key, headers)`` for OpenAI Content Provenance API.

    Resolution priority:
    1. Explicit ``tools.provenance_provider`` (dashboard provider name or ID).
    2. Enabled standalone tool secret (``SettingsDoc("tool.secrets")["check_provenance"]``).
    3. First enabled OpenAI-format provider in the dashboard pointing at ``api.openai.com``.
    4. Deployment-level ``OPENAI_API_KEY`` environment variable against ``https://api.openai.com/v1``.
    """
    if provider_ref.strip():
        p = _match_provider(provider_ref)
        if p is None:
            raise ProvenanceError(f"找不到已启用的提供商「{provider_ref}」，请检查 tools.provenance_provider 配置")
        if not p.api_key:
            raise ProvenanceError(f"提供商「{p.name}」未配置 API Key")
        return p.base_url.rstrip("/"), p.api_key, dict(p.headers)

    sec = _active_secret(secret)
    if sec is not None and sec.enabled:
        if not sec.api_key:
            raise ProvenanceError("check_provenance 已启用独立凭据但未配置 API Key")
        base = (sec.base_url or OPENAI_DEFAULT_BASE).rstrip("/")
        return base, sec.api_key, dict(sec.headers)

    for p in snapshot().providers.values():
        if p.enabled and p.api_format == "openai" and p.api_key and _is_official_openai(p.base_url):
            return p.base_url.rstrip("/"), p.api_key, dict(p.headers)

    if APP_CONFIG.openai_api_key.strip():
        return OPENAI_DEFAULT_BASE, APP_CONFIG.openai_api_key.strip(), {}

    raise ProvenanceError(
        "未配置支持 OpenAI Content Provenance 的 OpenAI 提供商"
        "（请在控制台「工具与技能」中配置独立凭据/选择提供商，或配置环境变量 OPENAI_API_KEY）"
    )


def _endpoint_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    parsed = urlparse(clean)
    if not parsed.path or parsed.path == "/":
        return f"{clean}/v1/content_provenance_checks"
    return f"{clean}/content_provenance_checks"


def _guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or fallback


def _parse_report(payload: dict[str, Any]) -> ProvenanceReport:
    raw_results = payload.get("results")
    signals: list[ProvenanceSignal] = []
    if isinstance(raw_results, list):
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            signals.append(
                ProvenanceSignal(
                    type=str(item.get("type") or "unknown"),
                    outcome=str(item.get("outcome") or "unknown"),
                    validation_state=(
                        str(item["validation_state"]) if item.get("validation_state") is not None else None
                    ),
                    issuer=str(item["issuer"]) if item.get("issuer") is not None else None,
                    model=str(item["model"]) if item.get("model") is not None else None,
                    generated_at=str(item["generated_at"]) if item.get("generated_at") is not None else None,
                )
            )
    created = payload.get("created_at")
    return ProvenanceReport(
        created_at=int(created) if isinstance(created, int) else None,
        results=tuple(signals),
    )


async def check_content_provenance(
    data: bytes,
    filename: str,
    *,
    mime: str = "",
    provider_ref: str = "",
    secret: ToolSecretInfo | None = None,
    timeout_s: float = PROVENANCE_TIMEOUT_S,
    max_bytes: int = PROVENANCE_MAX_BYTES,
    client: httpx.AsyncClient | None = None,
) -> ProvenanceReport:
    """Upload raw file bytes to ``POST /v1/content_provenance_checks`` and return parsed results."""
    if not data:
        raise ProvenanceError("文件内容为空，无法检测")
    limit = min(max_bytes, PROVENANCE_MAX_BYTES)
    if len(data) > limit:
        raise ProvenanceError(f"文件超过 {limit // (1024 * 1024)}MB 限制，无法上传检测")

    base_url, api_key, extra_headers = resolve_provenance_endpoint(provider_ref, secret=secret)
    url = _endpoint_url(base_url)
    content_type = mime.strip() or _guess_mime(filename)
    headers = {
        "User-Agent": APP_CONFIG.llm_user_agent,
        **extra_headers,
        "Authorization": f"Bearer {api_key}",
    }

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await http.post(
            url,
            headers=headers,
            files={"file": (filename or "file.bin", data, content_type)},
            timeout=timeout_s,
        )
    except httpx.HTTPError as e:
        raise ProvenanceError(f"请求 OpenAI Content Provenance API 失败：{e}") from e
    finally:
        if owns_client:
            await http.aclose()

    if resp.status_code >= 400:
        detail = ""
        try:
            err_body = resp.json()
            if isinstance(err_body, dict):
                err = err_body.get("error")
                detail = str(err.get("message") if isinstance(err, dict) else err or "")
        except ValueError:
            detail = resp.text[:200]
        raise ProvenanceError(
            f"OpenAI Content Provenance API 返回 HTTP {resp.status_code}：{detail or resp.text[:200]}"
        )

    try:
        body = resp.json()
    except ValueError as e:
        raise ProvenanceError(f"OpenAI Content Provenance API 返回了非 JSON 响应：{resp.text[:200]}") from e
    if not isinstance(body, dict):
        raise ProvenanceError("OpenAI Content Provenance API 返回格式异常")
    return _parse_report(body)


_SIGNAL_LABELS = {"c2pa": "C2PA 内容凭证", "synthid": "SynthID 水印"}
_OUTCOME_LABELS = {"detected": "已检测到", "not_detected": "未检测到"}
_STATE_LABELS = {"trusted": "签名可信 (trusted)", "valid": "签名有效 (valid)", "invalid": "签名无效 (invalid)"}


def format_provenance_report(report: ProvenanceReport, *, filename: str = "") -> str:
    """Format a :class:`ProvenanceReport` into clear Chinese text for the model."""
    header = f"OpenAI 内容溯源检测结果（{filename}）：" if filename else "OpenAI 内容溯源检测结果："
    if not report.results:
        return f"{header}\n未返回任何受支持的溯源信号结果（该文件类型可能不受支持，或未被检测到 OpenAI 溯源信息）。"
    lines = [header]
    for sig in report.results:
        sname = _SIGNAL_LABELS.get(sig.type.lower(), sig.type)
        outcome = _OUTCOME_LABELS.get(sig.outcome.lower(), sig.outcome)
        extras: list[str] = []
        if sig.validation_state:
            extras.append(f"校验={_STATE_LABELS.get(sig.validation_state.lower(), sig.validation_state)}")
        if sig.issuer:
            extras.append(f"签发方={sig.issuer}")
        if sig.model:
            extras.append(f"模型={sig.model}")
        if sig.generated_at:
            extras.append(f"生成时间={sig.generated_at}")
        detail = f"（{'，'.join(extras)}）" if extras else ""
        lines.append(f"- {sname}：{outcome}{detail}")
    if not report.any_detected:
        lines.append(
            "说明：未检测到 OpenAI 溯源信号并不等同于证明非 AI 生成——图片/文件经截图、转码或平台压缩后可能剥离 C2PA "
            "元数据或削弱水印，也可能是非 OpenAI 模型或旧版模型生成。"
        )
    return "\n".join(lines)
