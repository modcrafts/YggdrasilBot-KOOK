"""Dedicated Tools configuration & Skills management API (``/api/tools`` and ``/api/skills``).

Focuses on each tool's own parameters, credentials, live dependency diagnostics, online testing,
and built-in / custom skill management — separate from per-scope permission management in ``/channels``.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from yggdrasil.admin import settings_api
from yggdrasil.admin.auth import Actor
from yggdrasil.admin.common import Ok, change, conflict, not_found, unprocessable
from yggdrasil.agent.skills import kook as kook_skill
from yggdrasil.agent.skills import stickers as stickers_skill
from yggdrasil.config import APP_CONFIG
from yggdrasil.crypto import decrypt, encrypt, mask
from yggdrasil.db.models import EmbedSpace, Provider, SearchProvider, SettingsDoc, TaskRoute
from yggdrasil.db.session import get_session
from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.ops.tools import _KOOK_NEVER, OPS_TOOL_SPECS, OpsToolCategory
from yggdrasil.runtime import (
    GMOD_SERVERS_KEY,
    SKILLS_CONFIG_KEY,
    TOOL_SECRETS_KEY,
    ProviderInfo,
    ToolSecretInfo,
    _parse_gmod_servers,
)
from yggdrasil.settings import OpsRoleRule, OpsToolName, Settings, ToolName, ToolSettings, merge_settings
from yggdrasil.tools.azuriom import AzuriomConfigError, AzuriomError, health_check, resolve_azuriom_secret
from yggdrasil.tools.provenance import (
    OPENAI_DEFAULT_BASE,
    PROVENANCE_MAX_BYTES,
    ProvenanceError,
    check_content_provenance,
    describe_provenance_source,
    format_provenance_report,
)
from yggdrasil.tools.search import WEB_KINDS

router = APIRouter(tags=["tools"])

ToolCategory = Literal["web", "media", "memory", "render", "sandbox", "gmod", "azuriom"]
ToolStatus = Literal["ready", "warning", "unconfigured", "disabled"]
SkillHandler = Literal["chat", "tldr"]
ParamValueType = Literal["integer", "number", "boolean", "string", "enum"]

_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Minimal valid 1x1 transparent PNG used for lightweight connectivity probes when no file is uploaded.
_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Optional UI polish for ToolSettings fields: key -> (label, unit, group).
# Any new ToolSettings field NOT listed here still renders automatically from its Pydantic schema.
_PARAM_UI_META: dict[str, tuple[str, str, str]] = {
    "read_url_max_chars": ("单页最大字符数", "字", ""),
    "browser_fallback": ("浏览器渲染回退", "", ""),
    "remote_fetch": ("DeepSeek 远程抓取兜底", "", ""),
    "search_results": ("默认返回结果数", "条", ""),
    "search_x_default_hours": ("默认回溯时间", "小时", ""),
    "provenance_provider": ("指定提供商", "", ""),
    "azuriom_timeout_s": ("接口请求超时", "秒", ""),
    "provenance_timeout_s": ("请求超时时间", "秒", ""),
    "provenance_max_mb": ("单文件上传上限", "MB", ""),
    "file_max_mb": ("附件下载上限", "MB", ""),
    "file_max_pages": ("PDF 最大解析页数", "页", ""),
    "file_max_chars": ("提取正文最大字符数", "字", ""),
    "history_top_k": ("检索返回条数", "条", ""),
    "messages_around_max": ("单侧最大拉取条数", "条", ""),
    "glossary_list_max": ("词条总览返回上限", "条", ""),
    "render_max_source_chars": ("源码最大字符数", "字", ""),
    "render_timeout_s": ("渲染超时时间", "秒", ""),
    "render_markdown_max_pages": ("单次最多渲染页数", "页", ""),
    "generate_image_max_references": ("参考图最大张数", "张", ""),
    "kook_upload_max_mb": ("upload_file 上传上限", "MB", ""),
    "kook_pinned_shown": ("置顶消息展示条数", "条", ""),
    "kook_thread_body_chars": ("帖子主楼正文截断长度", "字", ""),
    "stickers_pool_limit": ("表情包候选池上限", "张", ""),
    "sandbox_enabled": ("工作区全局总开关", "", "基础与命令"),
    "sandbox_command_timeout_s": ("单次命令默认超时", "秒", "基础与命令"),
    "sandbox_command_max_s": ("单次命令最长超时", "秒", "基础与命令"),
    "sandbox_invocation_s": ("单次请求累计执行上限", "秒", "基础与命令"),
    "sandbox_max_commands": ("单次请求最大命令次数", "次", "基础与命令"),
    "sandbox_idle_min": ("闲置回收时间", "分钟", "生命周期与配额"),
    "sandbox_max_age_min": ("最长存活时间", "分钟", "生命周期与配额"),
    "sandbox_max_workspaces": ("全局工作区总数上限", "个", "生命周期与配额"),
    "sandbox_max_workspaces_per_channel": ("单频道工作区上限", "个", "生命周期与配额"),
    "sandbox_max_links": ("单工作区公开链接上限", "个", "生命周期与配额"),
    "sandbox_request_limit": ("有工作区时模型请求上限", "次", "预算与桥接"),
    "sandbox_tool_calls_limit": ("有工作区时工具调用上限", "次", "预算与桥接"),
    "bridge_run_limit": ("单次对话工具桥接上限", "次", "预算与桥接"),
    "bridge_job_limit": ("后台任务工具桥接上限", "次", "预算与桥接"),
    "sandbox_max_jobs": ("每频道并发后台任务上限", "个", "后台任务"),
    "sandbox_job_command_min": ("后台命令最长运行时间", "分钟", "后台任务"),
    "sandbox_delegate": ("启用后台大任务（delegate）", "", "后台任务"),
    "sandbox_job_agent_min": ("后台大任务最长运行时间", "分钟", "后台任务"),
    "sandbox_job_max_input_tokens": ("后台大任务输入 Token 上限", "", "后台任务"),
    "sandbox_job_max_output_tokens": ("后台大任务输出 Token 上限", "", "后台任务"),
}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    id: ToolName
    name: str
    category: ToolCategory
    description: str
    handlers: tuple[str, ...]
    param_keys: tuple[str, ...] = ()
    supports_secret: bool = False
    secret_default_base_url: str = ""
    secret_hint: str = ""
    provider_param_key: str | None = None
    related_route: str | None = None
    related_label: str | None = None


@dataclass(frozen=True, slots=True)
class BuiltinSkillSpec:
    id: str
    name: str
    description: str
    tools: tuple[str, ...]
    param_keys: tuple[str, ...]


_SANDBOX_PARAM_KEYS: tuple[str, ...] = (
    "sandbox_enabled",
    "sandbox_command_timeout_s",
    "sandbox_command_max_s",
    "sandbox_invocation_s",
    "sandbox_max_commands",
    "sandbox_idle_min",
    "sandbox_max_age_min",
    "sandbox_max_workspaces",
    "sandbox_max_workspaces_per_channel",
    "sandbox_max_links",
    "sandbox_request_limit",
    "sandbox_tool_calls_limit",
    "sandbox_max_jobs",
    "sandbox_job_command_min",
    "sandbox_delegate",
    "sandbox_job_agent_min",
    "sandbox_job_max_input_tokens",
    "sandbox_job_max_output_tokens",
    "bridge_run_limit",
    "bridge_job_limit",
)

TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        id="read_url",
        name="网页阅读",
        category="web",
        description="读取网页正文（支持分页、浏览器渲染回退、DeepSeek 远程抓取）与 KOOK 消息/帖子锚点链接。",
        handlers=("chat", "tldr"),
        param_keys=("read_url_max_chars", "browser_fallback", "remote_fetch"),
        related_route="/search",
        related_label="搜索源",
    ),
    ToolSpec(
        id="web_search",
        name="网页搜索",
        category="web",
        description="按优先级轮询已配置的搜索引擎（Exa、DeepSeek、SearXNG）检索最新资料。",
        handlers=("chat", "tldr"),
        param_keys=("search_results",),
        related_route="/search",
        related_label="搜索源",
    ),
    ToolSpec(
        id="read_x",
        name="读取 X 帖子",
        category="web",
        description="读取 X（Twitter）帖子全文、引用、投票、社区笔记与配图句柄。",
        handlers=("chat", "tldr"),
        param_keys=("read_url_max_chars",),
        related_route="/search",
        related_label="搜索源",
    ),
    ToolSpec(
        id="search_x",
        name="搜索 X 动态",
        category="web",
        description="通过 Grok 或 X API 搜索 X（Twitter）上的最新新闻摘要与讨论。",
        handlers=("chat", "tldr"),
        param_keys=("search_x_default_hours", "search_results"),
        related_route="/search",
        related_label="搜索源",
    ),
    ToolSpec(
        id="check_provenance",
        name="OpenAI 内容溯源检测",
        category="media",
        description="调用 OpenAI /v1/content_provenance_checks 检测图片、音频或 PDF 中的 C2PA 凭证与 SynthID 水印。",
        handlers=("chat",),
        param_keys=("provenance_provider", "provenance_timeout_s", "provenance_max_mb"),
        supports_secret=True,
        secret_default_base_url=OPENAI_DEFAULT_BASE,
        secret_hint="留空默认使用 https://api.openai.com/v1；如使用代理请填完整 https:// 地址。",
        provider_param_key="provenance_provider",
        related_route="/providers",
        related_label="Providers",
    ),
    ToolSpec(
        id="read_file",
        name="文档与附件读取",
        category="media",
        description="解析 PDF（含扫描版转图）、Office 文档、代码、纯文本与压缩包目录。",
        handlers=("chat", "tldr"),
        param_keys=("file_max_mb", "file_max_pages", "file_max_chars"),
        related_route="/routes",
        related_label="模型路由",
    ),
    ToolSpec(
        id="view_image",
        name="查看图片",
        category="media",
        description="查看聊天或网页中的图片；主模型不支持看图时自动回退到 vision 路由。",
        handlers=("chat", "tldr"),
        param_keys=(),
        related_route="/routes",
        related_label="模型路由",
    ),
    ToolSpec(
        id="search_history",
        name="搜索历史聊天",
        category="memory",
        description="关键词 + 语义混合检索本服务器共享频道的历史聊天记录、话题与每日摘要。",
        handlers=("chat", "tldr"),
        param_keys=("history_top_k",),
        related_route="/routes",
        related_label="向量空间",
    ),
    ToolSpec(
        id="get_messages_around",
        name="查看消息上下文",
        category="memory",
        description="按消息编号 #id 拉取前后若干条上下文消息，支持同服务器共享频道。",
        handlers=("chat", "tldr"),
        param_keys=("messages_around_max",),
    ),
    ToolSpec(
        id="recent_chat",
        name="近期聊天记录",
        category="memory",
        description="读取当前频道或同服务器其他可见频道最近一段时间的聊天记录。",
        handlers=("chat", "tldr"),
        param_keys=(),
        related_route="/settings",
        related_label="上下文设置",
    ),
    ToolSpec(
        id="lookup_glossary",
        name="黑话词典查询",
        category="memory",
        description="查询本服务器自动沉淀的黑话/梗词典释义或列出全部词条。",
        handlers=("chat", "tldr"),
        param_keys=("glossary_list_max",),
        related_route="/memory",
        related_label="记忆",
    ),
    ToolSpec(
        id="get_profile",
        name="成员画像查询",
        category="memory",
        description="查询同服务器同步的成员昵称、兴趣、特点与重要事实画像。",
        handlers=("chat",),
        param_keys=(),
        related_route="/memory",
        related_label="记忆",
    ),
    ToolSpec(
        id="render_mermaid",
        name="Mermaid 渲染成图",
        category="render",
        description="将 Mermaid 图表源码通过渲染服务导出为 PNG 图片句柄。",
        handlers=("chat", "tldr"),
        param_keys=("render_max_source_chars", "render_timeout_s"),
    ),
    ToolSpec(
        id="render_markdown",
        name="Markdown 渲染成图",
        category="render",
        description="将复杂表格、数学公式或长文档渲染为多页 PNG 图片句柄。",
        handlers=("chat", "tldr"),
        param_keys=("render_max_source_chars", "render_timeout_s", "render_markdown_max_pages"),
    ),
    ToolSpec(
        id="render_html",
        name="HTML 渲染成图",
        category="render",
        description="将内联样式 HTML 卡片/排版片段渲染为 PNG 图片句柄。",
        handlers=("chat", "tldr"),
        param_keys=("render_max_source_chars", "render_timeout_s"),
    ),
    ToolSpec(
        id="generate_image",
        name="AI 画图",
        category="render",
        description="按文本提示词生成图片或基于参考图改图并直接发送到频道（默认关闭）。",
        handlers=("chat",),
        param_keys=("generate_image_max_references",),
        related_route="/routes",
        related_label="模型路由",
    ),
    ToolSpec(
        id="gmod_queue_lookup",
        name="GMod 玩家排队查询",
        category="gmod",
        description="按 SteamID 或 SteamID64 查看玩家排队位次和进服状态，通行证仅本人可查。",
        handlers=("chat",),
    ),
    ToolSpec(
        id="gmod_queue_list",
        name="GMod 队列查询",
        category="gmod",
        description="查看游戏服人数与容量，可选排队名单、本人通行证或队列详情。",
        handlers=("chat",),
    ),
    ToolSpec(
        id="azuriom_lookup",
        name="Azuriom 绑定查询",
        category="azuriom",
        description="查询 KOOK、Steam 与 Azuriom 网站账号的绑定关系。Steam 输入支持 SteamID64 或 STEAM_0/1:X:Y。",
        handlers=("chat",),
        param_keys=("azuriom_timeout_s",),
        supports_secret=True,
        secret_default_base_url="https://example.com",
        secret_hint="填写 Azuriom 网站根地址（如 https://example.com），"
        "API Key 填 Ygg Bridge 插件后台生成的 Bearer Token；普通对话与管理助手（ops）共用此凭据。",
    ),
    ToolSpec(
        id="sandbox",
        name="沙盒工作区套件",
        category="sandbox",
        description="隔离的 Linux 工作区，提供 bash、常驻 Python kernel、文件读写、公开端口链接与后台任务。",
        handlers=("chat",),
        param_keys=_SANDBOX_PARAM_KEYS,
        related_route="/sandbox",
        related_label="工作区",
    ),
)

TOOL_SPEC_MAP: dict[str, ToolSpec] = {spec.id: spec for spec in TOOL_SPECS}

BUILTIN_SKILL_SPECS: tuple[BuiltinSkillSpec, ...] = (
    BuiltinSkillSpec(
        id=kook_skill.ID,
        name="KOOK 服务器与频道能力",
        description=kook_skill.DESCRIPTION,
        tools=(
            "member_info",
            "channel_info",
            "get_avatar",
            "react_message",
            "upload_file",
            "list_forum_threads",
            "read_forum_thread",
        ),
        param_keys=("kook_upload_max_mb", "kook_pinned_shown", "kook_thread_body_chars"),
    ),
    BuiltinSkillSpec(
        id=stickers_skill.ID,
        name="表情包检索与收藏",
        description=stickers_skill.DESCRIPTION,
        tools=("search_stickers", "pick_sticker", "save_sticker"),
        param_keys=("stickers_pool_limit",),
    ),
)

BUILTIN_SKILL_MAP: dict[str, BuiltinSkillSpec] = {spec.id: spec for spec in BUILTIN_SKILL_SPECS}


# --- Pydantic Schemas -----------------------------------------------------------------


class ToolSecretOut(BaseModel):
    enabled: bool = False
    base_url: str = ""
    api_key_masked: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


class ToolSecretIn(BaseModel):
    enabled: bool = False
    base_url: str = Field(default="", pattern=r"^(https?://.*)?$")
    # Empty / omitted keeps the stored encrypted key on update.
    api_key: str | None = None
    clear_api_key: bool = False
    headers: dict[str, str] = Field(default_factory=dict)


class ToolParamFieldOut(BaseModel):
    key: str
    label: str
    type: ParamValueType
    description: str = ""
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    options: list[str] = Field(default_factory=list)
    unit: str = ""
    group: str = ""


class ToolItemOut(BaseModel):
    id: ToolName
    name: str
    category: ToolCategory
    description: str
    handlers: list[str]
    enabled: bool
    slow: bool
    status: ToolStatus
    status_label: str
    status_detail: str
    related_route: str | None = None
    related_label: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    param_fields: list[ToolParamFieldOut] = Field(default_factory=list)
    supports_secret: bool = False
    secret_default_base_url: str = ""
    secret_hint: str = ""
    provider_param_key: str | None = None
    secret: ToolSecretOut | None = None


class GeneralToolPolicyOut(BaseModel):
    request_limit: int
    tool_calls_limit: int
    slow_tools: list[str]


class GeneralToolPolicyIn(BaseModel):
    request_limit: int | None = Field(default=None, ge=1, le=100)
    tool_calls_limit: int | None = Field(default=None, ge=1, le=200)
    slow_tools: list[str] | None = None


class ToolsOverviewOut(BaseModel):
    items: list[ToolItemOut]
    general: GeneralToolPolicyOut
    runtime_flags: dict[str, bool]


class ToolConfigPatchIn(BaseModel):
    enabled: bool | None = None
    slow: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    secret: ToolSecretIn | None = None


class ProvenanceTestIn(BaseModel):
    """Optional base64-encoded file for real provenance testing; omit to run a lightweight 1x1 PNG probe."""

    file_b64: str | None = None
    filename: str = ""
    mime: str = ""


class ProvenanceTestOut(BaseModel):
    ok: bool
    latency_ms: int
    source: str
    endpoint: str = ""
    filename: str = ""
    any_detected: bool | None = None
    report: str | None = None
    error: str | None = None


class AzuriomTestOut(BaseModel):
    ok: bool
    latency_ms: int
    endpoint: str = ""
    plugin: str = ""
    version: str = ""
    game: str | None = None
    kook_accounts_ready: bool | None = None
    kook_account_model: bool | None = None
    error: str | None = None


class BuiltinSkillOut(BaseModel):
    id: str
    name: str
    description: str
    tools: list[str]
    enabled: bool
    params: dict[str, Any] = Field(default_factory=dict)
    defaults: dict[str, Any] = Field(default_factory=dict)
    param_fields: list[ToolParamFieldOut] = Field(default_factory=list)
    extra_instructions: str = ""


class BuiltinSkillPatchIn(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    extra_instructions: str | None = None


class CustomSkillIn(BaseModel):
    id: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(min_length=1, max_length=32000)
    defer_loading: bool = True
    handlers: list[SkillHandler] = Field(default_factory=lambda: ["chat"])
    allow_private: bool = True
    enabled: bool = True
    default_active: bool = True


class CustomSkillUpdateIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=1000)
    instructions: str = Field(min_length=1, max_length=32000)
    defer_loading: bool = True
    handlers: list[SkillHandler] = Field(default_factory=lambda: ["chat"])
    allow_private: bool = True
    enabled: bool = True
    default_active: bool = True


class CustomSkillOut(CustomSkillIn):
    pass


class SkillsOverviewOut(BaseModel):
    builtin: list[BuiltinSkillOut]
    custom: list[CustomSkillOut]


class OpsToolOut(BaseModel):
    id: OpsToolName
    name: str
    category: OpsToolCategory
    write: bool
    global_only: bool
    description: str


class OpsToolsOverviewOut(BaseModel):
    tools: list[OpsToolOut]
    kook_write_actions: list[str]
    never_allowed: list[str]
    role_rules: dict[str, OpsRoleRule]


class OpsToolsConfigIn(BaseModel):
    kook_write_actions: list[str] | None = None
    role_rules: dict[str, OpsRoleRule] | None = None


# --- Helpers --------------------------------------------------------------------------


def _param_fields(param_keys: tuple[str, ...], *, exclude: set[str] | None = None) -> list[ToolParamFieldOut]:
    """Build UI field descriptors directly from ``ToolSettings`` JSON schema so new backend params auto-render."""
    schema_props = ToolSettings.model_json_schema().get("properties", {})
    out: list[ToolParamFieldOut] = []
    skip = exclude or set()
    for key in param_keys:
        if key in skip:
            continue
        prop = schema_props.get(key)
        if not isinstance(prop, dict):
            continue
        if isinstance(prop.get("anyOf"), list):
            non_null = [v for v in prop["anyOf"] if isinstance(v, dict) and v.get("type") != "null"]
            if len(non_null) == 1:
                resolved = {**non_null[0], **{k: v for k, v in prop.items() if k != "anyOf"}}
            else:
                resolved = prop
        else:
            resolved = prop

        options = [str(x) for x in resolved["enum"]] if isinstance(resolved.get("enum"), list) else []
        raw_type = resolved.get("type")
        if options:
            ftype: ParamValueType = "enum"
        elif raw_type == "boolean":
            ftype = "boolean"
        elif raw_type == "integer":
            ftype = "integer"
        elif raw_type == "number":
            ftype = "number"
        else:
            ftype = "string"

        label, unit, group = _PARAM_UI_META.get(key, (key, "", ""))
        minimum = resolved.get("minimum")
        maximum = resolved.get("maximum")
        out.append(
            ToolParamFieldOut(
                key=key,
                label=label,
                type=ftype,
                description=str(resolved.get("description") or ""),
                default=resolved.get("default"),
                minimum=float(minimum) if isinstance(minimum, int | float) else None,
                maximum=float(maximum) if isinstance(maximum, int | float) else None,
                options=options,
                unit=unit,
                group=group,
            )
        )
    return out


def _sparse_diff(value: Any, base: Any) -> Any:
    if isinstance(value, dict) and isinstance(base, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            diff = _sparse_diff(v, base.get(k))
            if diff is not _SAME:
                out[k] = diff
        return out if out else _SAME
    return _SAME if value == base else value


_SAME = object()


def _masked_secret(raw: dict[str, Any] | None) -> ToolSecretOut:
    if not isinstance(raw, dict):
        return ToolSecretOut()
    masked: str | None = None
    enc_raw = raw.get("api_key_enc")
    if isinstance(enc_raw, str) and enc_raw:
        try:
            masked = mask(decrypt(enc_raw.encode()))
        except Exception:
            masked = "…(无法解密)"
    headers_raw = raw.get("headers")
    headers = (
        {str(k): str(v) for k, v in headers_raw.items() if str(k).strip()} if isinstance(headers_raw, dict) else {}
    )
    return ToolSecretOut(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url") or ""),
        api_key_masked=masked,
        headers=headers,
    )


def _secret_info(raw: dict[str, Any] | None) -> ToolSecretInfo:
    if not isinstance(raw, dict):
        return ToolSecretInfo()
    api_key: str | None = None
    enc_raw = raw.get("api_key_enc")
    if isinstance(enc_raw, str) and enc_raw:
        try:
            api_key = decrypt(enc_raw.encode())
        except Exception:
            api_key = None
    headers_raw = raw.get("headers")
    headers = (
        {str(k): str(v) for k, v in headers_raw.items() if str(k).strip()} if isinstance(headers_raw, dict) else {}
    )
    return ToolSecretInfo(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url") or "").strip().rstrip("/"),
        api_key=api_key,
        headers=headers,
    )


def _provider_info(p: Provider) -> ProviderInfo:
    key: str | None = None
    if p.api_key_enc:
        try:
            key = decrypt(p.api_key_enc)
        except Exception:
            key = None
    return ProviderInfo(
        p.id,
        p.name,
        p.base_url.rstrip("/"),
        key,
        dict(p.headers),
        p.enabled,
        tuple(str(m) for m in (p.models or ())),
        p.api_format,
        {m: ModelCaps.model_validate(c) for m, c in (p.model_caps or {}).items()},
    )


def _runtime_flags() -> dict[str, bool]:
    return {
        "browser_configured": bool(APP_CONFIG.browser_url),
        "render_configured": bool(APP_CONFIG.render_url),
        "sandbox_configured": bool(APP_CONFIG.sandbox_token),
        "sandbox_links_configured": bool(APP_CONFIG.sandbox_link_domain),
        "relay_configured": bool(APP_CONFIG.relay_url),
        "openai_env_key_configured": bool(APP_CONFIG.openai_api_key.strip()),
    }


def _describe_provenance_from_db(
    provider_ref: str,
    secret: ToolSecretInfo,
    providers: dict[int, ProviderInfo],
) -> tuple[ToolStatus, str, str]:
    ref = provider_ref.strip()
    if ref:
        matched: ProviderInfo | None = None
        if ref.isdigit() and (cand := providers.get(int(ref))) is not None and cand.enabled:
            matched = cand
        else:
            ref_l = ref.lower()
            for cand in providers.values():
                if cand.enabled and cand.name.strip().lower() == ref_l:
                    matched = cand
                    break
        if matched is None:
            return "unconfigured", "Provider 无效", f"找不到已启用的提供商「{ref}」"
        if not matched.api_key:
            return "unconfigured", "缺少 API Key", f"提供商「{matched.name}」未配置 API Key"
        return "ready", f"复用 {matched.name}", f"使用 Provider「{matched.name}」（{matched.base_url}）"

    if secret.enabled:
        base = secret.base_url or OPENAI_DEFAULT_BASE
        if not secret.api_key:
            return "unconfigured", "缺少独立 Key", f"已启用独立凭据（{base}），但未填写 API Key"
        return "ready", "独立凭据", f"使用独立加密凭据（{base}）"

    for cand in providers.values():
        if cand.enabled and cand.api_format == "openai" and cand.api_key and "api.openai.com" in cand.base_url.lower():
            return "ready", f"自动匹配 {cand.name}", f"自动匹配 OpenAI 官方提供商「{cand.name}」"

    if APP_CONFIG.openai_api_key.strip():
        return "ready", "环境变量", "使用部署环境变量 OPENAI_API_KEY"

    return "unconfigured", "未配置", "请选择 OpenAI 提供商、填写独立凭据，或配置环境变量 OPENAI_API_KEY"


def _diagnose_tool(
    spec: ToolSpec,
    effective: Settings,
    *,
    providers: dict[int, ProviderInfo],
    search_providers: list[SearchProvider],
    routes: dict[str, TaskRoute],
    has_embed: bool,
    enabled_gmod_servers: int,
    secret_raw: dict[str, Any] | None,
) -> tuple[ToolStatus, str, str]:
    enabled = spec.id in effective.tools.enabled
    if not enabled:
        if spec.id == "generate_image":
            return "disabled", "默认关闭", "需配置 image 模型路由后按需开启"
        return "disabled", "全局默认关闭", "可在「服务器与频道」单独覆盖开启"

    if spec.id == "azuriom_lookup":
        sec = _secret_info(secret_raw)
        try:
            resolve_azuriom_secret(sec)
        except AzuriomConfigError as e:
            return "unconfigured", str(e).split("，", 1)[0], str(e)
        return "ready", "独立凭据", "已配置 Ygg Bridge 网站地址与 API Token"

    if spec.category == "gmod":
        if not enabled_gmod_servers:
            return "unconfigured", "未配置 GMod 服务器", "请前往「GMod 通信」标签页添加并启用服务器"
        return "ready", "GMod 通信已配置", f"已启用 {enabled_gmod_servers} 台 GMod 服务器"

    t = effective.tools
    enabled_search = [s for s in search_providers if s.enabled]

    if spec.id == "read_url":
        extras = []
        if t.browser_fallback:
            extras.append("浏览器回退已就绪" if APP_CONFIG.browser_url else "浏览器未部署(YGG_BROWSER_URL)")
        if t.remote_fetch:
            has_ds = any(s.kind == "deepseek" for s in enabled_search)
            extras.append("DeepSeek 远程抓取就绪" if has_ds else "未配 DeepSeek 搜索源")
        detail = "HTTP 直接抓取就绪" + (f"；{'，'.join(extras)}" if extras else "")
        return "ready", "已就绪", detail

    if spec.id == "web_search":
        web_list = [s.name for s in enabled_search if s.kind in WEB_KINDS]
        if not web_list:
            return "unconfigured", "无可用搜索源", "请在「搜索源」添加 Exa、DeepSeek 或 SearXNG"
        return "ready", f"{len(web_list)} 个搜索源", f"当前可用：{'、'.join(web_list)}"

    if spec.id == "read_x":
        has_x = any(s.kind == "x" for s in enabled_search)
        if has_x:
            return "ready", "X API 已配置", "优先使用 X API 读取帖子，失败时自动回退公开抓取"
        return "ready", "公开抓取模式", "未配置 X API（可选），当前使用公开接口读取帖子"

    if spec.id == "search_x":
        has_grok = any(s.kind == "grok" for s in enabled_search)
        has_x = any(s.kind == "x" for s in enabled_search)
        if has_grok and has_x:
            return "ready", "Grok + X API", "优先使用 Grok 搜索 X，失败自动回退 X API"
        if has_grok:
            return "ready", "Grok 已就绪", "使用 Grok 搜索 X 动态"
        if has_x:
            return "ready", "X API 已就绪", "使用 X API 搜索新闻与原帖"
        return "unconfigured", "未配置搜索源", "请在「搜索源」添加 Grok 或 X API 类型"

    if spec.id == "check_provenance":
        return _describe_provenance_from_db(t.provenance_provider, _secret_info(secret_raw), providers)

    if spec.id == "read_file":
        has_vision = "vision" in routes
        detail = f"上限 {t.file_max_mb}MB / {t.file_max_pages} 页 / {t.file_max_chars} 字"
        if not has_vision:
            detail += "（未配 vision 路由，扫描 PDF 依赖主模型看图能力）"
        return "ready", "已就绪", detail

    if spec.id == "view_image":
        chat_route = routes.get("chat")
        chat_vision = False
        if chat_route and (p := providers.get(chat_route.provider_id)):
            caps = p.caps.get(chat_route.model)
            chat_vision = bool(caps and caps.vision)
        has_vision_route = "vision" in routes
        if chat_vision and has_vision_route:
            return (
                "ready",
                "原生看图 + 兜底",
                f"主对话模型支持看图，且已配置 vision 路由（inline_images={effective.context.inline_images}）",
            )
        if chat_vision:
            return "ready", "原生看图", f"主对话模型直接看图（inline_images={effective.context.inline_images}）"
        if has_vision_route:
            return "ready", "vision 路由看图", "通过 vision 模型路由转述图片内容"
        return "warning", "看图能力未知", "主对话模型未标明 vision 能力，建议在「模型路由」配置 vision 兜底"

    if spec.id == "search_history":
        parts = ["BM25 关键词检索已就绪"]
        parts.append("向量语义检索已就绪" if has_embed else "未配置生效向量空间")
        if "rerank" in routes:
            parts.append("已开启重排")
        status: ToolStatus = "ready" if has_embed else "warning"
        return status, ("混合检索就绪" if has_embed else "仅关键词检索"), "；".join(parts)

    if spec.id in ("get_messages_around", "recent_chat"):
        return "ready", "已就绪", "读取本服务器可访问频道的数据库聊天记录"

    if spec.id in ("lookup_glossary", "get_profile"):
        if not effective.memory.enabled:
            return "warning", "记忆流水线已关闭", "全局 memory.enabled 为 false，历史沉淀不会继续更新"
        return "ready", "已就绪", "记忆流水线运行中"

    if spec.id in ("render_mermaid", "render_markdown", "render_html"):
        if not APP_CONFIG.render_url:
            return "unconfigured", "未配置渲染服务", "需部署渲染服务并配置环境变量 YGG_RENDER_URL"
        return "ready", "渲染服务就绪", f"超时 {t.render_timeout_s}s，源码上限 {t.render_max_source_chars} 字符"

    if spec.id == "generate_image":
        img_route = routes.get("image")
        if img_route is None:
            return "unconfigured", "未配置 image 路由", "请在「模型路由」配置 image 任务路由"
        pname = (
            providers[img_route.provider_id].name if img_route.provider_id in providers else f"#{img_route.provider_id}"
        )
        return (
            "ready",
            "画图路由就绪",
            f"使用 {pname} / {img_route.model}（参考图上限 {t.generate_image_max_references} 张）",
        )

    if spec.id == "sandbox":
        if not APP_CONFIG.sandbox_token:
            return "unconfigured", "未部署沙盒", "需配置环境变量 YGG_SANDBOX_TOKEN 启用工作区后端"
        if not t.sandbox_enabled:
            return "disabled", "总开关已关闭", "tools.sandbox_enabled 为 false，所有频道均不挂载工作区"
        notes = [f"全局上限 {t.sandbox_max_workspaces} 个 / 每频道 {t.sandbox_max_workspaces_per_channel} 个"]
        if APP_CONFIG.sandbox_link_domain:
            notes.append("支持公开链接")
        if t.sandbox_delegate and APP_CONFIG.relay_url and "agent" in routes:
            notes.append("delegate 后台大任务就绪")
        elif t.sandbox_delegate:
            notes.append("delegate 需 YGG_RELAY_URL + agent 路由")
        return "ready", "工作区就绪", "；".join(notes)

    if spec.supports_secret:
        sec = _secret_info(secret_raw)
        if sec.enabled and sec.api_key:
            base = sec.base_url or spec.secret_default_base_url or "默认地址"
            return "ready", "独立凭据已配置", f"使用独立加密凭据（{base}）"
        return "unconfigured", "未配置凭据", "请在工具配置中启用并填写独立 API Key"

    return "ready", "已就绪", ""


async def _load_tools_overview() -> ToolsOverviewOut:
    async with get_session() as s:
        global_row = await s.get(SettingsDoc, "global")
        secrets_row = await s.get(SettingsDoc, TOOL_SECRETS_KEY)
        gmod_row = await s.get(SettingsDoc, GMOD_SERVERS_KEY)
        provider_rows = (await s.scalars(select(Provider))).all()
        search_stmt = select(SearchProvider).order_by(SearchProvider.priority, SearchProvider.id)
        search_rows = list((await s.scalars(search_stmt)).all())
        route_rows = (await s.scalars(select(TaskRoute))).all()
        active_embed = await s.scalar(select(EmbedSpace.id).where(EmbedSpace.active))

    global_val = dict(global_row.value) if global_row and isinstance(global_row.value, dict) else {}
    secrets_val = dict(secrets_row.value) if secrets_row and isinstance(secrets_row.value, dict) else {}
    effective = merge_settings(global_val)
    defaults = Settings()

    gmod_servers = _parse_gmod_servers(gmod_row.value if gmod_row else {})
    enabled_gmod_servers = sum(server.enabled for server in gmod_servers.values())
    providers = {p.id: _provider_info(p) for p in provider_rows}
    routes = {r.task: r for r in route_rows}
    eff_tools = effective.tools.model_dump(mode="json")
    def_tools = defaults.tools.model_dump(mode="json")
    enabled_set = set(effective.tools.enabled)
    slow_set = set(effective.tools.slow_tools)

    items: list[ToolItemOut] = []
    for spec in TOOL_SPECS:
        sec_raw = secrets_val.get(spec.id) if spec.supports_secret else None
        st, label, detail = _diagnose_tool(
            spec,
            effective,
            providers=providers,
            search_providers=search_rows,
            routes=routes,
            has_embed=active_embed is not None,
            enabled_gmod_servers=enabled_gmod_servers,
            secret_raw=sec_raw if isinstance(sec_raw, dict) else None,
        )
        items.append(
            ToolItemOut(
                id=spec.id,
                name=spec.name,
                category=spec.category,
                description=spec.description,
                handlers=list(spec.handlers),
                enabled=spec.id in enabled_set,
                slow=spec.id in slow_set,
                status=st,
                status_label=label,
                status_detail=detail,
                related_route=spec.related_route,
                related_label=spec.related_label,
                params={k: eff_tools[k] for k in spec.param_keys},
                defaults={k: def_tools[k] for k in spec.param_keys},
                param_fields=_param_fields(
                    spec.param_keys,
                    exclude={spec.provider_param_key} if spec.provider_param_key else None,
                ),
                supports_secret=spec.supports_secret,
                secret_default_base_url=spec.secret_default_base_url,
                secret_hint=spec.secret_hint,
                provider_param_key=spec.provider_param_key,
                secret=_masked_secret(sec_raw if isinstance(sec_raw, dict) else None) if spec.supports_secret else None,
            )
        )

    return ToolsOverviewOut(
        items=items,
        general=GeneralToolPolicyOut(
            request_limit=effective.tools.request_limit,
            tool_calls_limit=effective.tools.tool_calls_limit,
            slow_tools=list(effective.tools.slow_tools),
        ),
        runtime_flags=_runtime_flags(),
    )


async def _load_skills_overview() -> SkillsOverviewOut:
    async with get_session() as s:
        global_row = await s.get(SettingsDoc, "global")
        skills_row = await s.get(SettingsDoc, SKILLS_CONFIG_KEY)

    global_val = dict(global_row.value) if global_row and isinstance(global_row.value, dict) else {}
    skills_val = dict(skills_row.value) if skills_row and isinstance(skills_row.value, dict) else {}
    effective = merge_settings(global_val)
    defaults = Settings()

    eff_tools = effective.tools.model_dump(mode="json")
    def_tools = defaults.tools.model_dump(mode="json")
    enabled_skills = set(effective.tools.skills)
    raw_overrides = skills_val.get("builtin_overrides")
    overrides = raw_overrides if isinstance(raw_overrides, dict) else {}

    builtin_out: list[BuiltinSkillOut] = []
    for spec in BUILTIN_SKILL_SPECS:
        ov = overrides.get(spec.id)
        extra = str(ov.get("extra_instructions") or "") if isinstance(ov, dict) else ""
        builtin_out.append(
            BuiltinSkillOut(
                id=spec.id,
                name=spec.name,
                description=spec.description,
                tools=list(spec.tools),
                enabled=spec.id in enabled_skills,
                params={k: eff_tools[k] for k in spec.param_keys},
                defaults={k: def_tools[k] for k in spec.param_keys},
                param_fields=_param_fields(spec.param_keys),
                extra_instructions=extra,
            )
        )

    custom_out: list[CustomSkillOut] = []
    for raw in skills_val.get("custom") or ():
        if not isinstance(raw, dict):
            continue
        try:
            custom_out.append(CustomSkillOut.model_validate(raw))
        except Exception:
            continue

    return SkillsOverviewOut(builtin=builtin_out, custom=custom_out)


def _save_global_section_patch(existing_global: dict[str, Any], section: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``patch`` into one top-level section of global settings, validate, and return clean sparse doc."""
    base_defaults = Settings().model_dump(mode="json")
    merged = merge_settings(existing_global).model_dump(mode="json")
    merged_sec = dict(merged[section])
    merged_sec.update(patch)
    merged[section] = merged_sec
    sparse = _sparse_diff(merged, base_defaults)
    clean_doc = sparse if isinstance(sparse, dict) else {}
    settings_api.validate_layers(clean_doc, check=clean_doc)
    return clean_doc


def _save_global_tools_patch(existing_global: dict[str, Any], tools_patch: dict[str, Any]) -> dict[str, Any]:
    """Merge ``tools_patch`` into global settings, validate via ``Settings``, and return clean sparse doc."""
    return _save_global_section_patch(existing_global, "tools", tools_patch)


async def _load_ops_overview() -> OpsToolsOverviewOut:
    async with get_session() as s:
        global_row = await s.get(SettingsDoc, "global")
    global_val = dict(global_row.value) if global_row and isinstance(global_row.value, dict) else {}
    effective = merge_settings(global_val)
    return OpsToolsOverviewOut(
        tools=[
            OpsToolOut(
                id=spec.id,
                name=spec.name,
                category=spec.category,
                write=spec.write,
                global_only=spec.global_only,
                description=spec.description,
            )
            for spec in OPS_TOOL_SPECS
        ],
        kook_write_actions=list(effective.ops.kook_write_actions),
        never_allowed=sorted(_KOOK_NEVER),
        role_rules=dict(effective.ops.role_rules),
    )


def _validate_custom_skill(skill: CustomSkillIn | CustomSkillUpdateIn, *, skill_id: str | None = None) -> None:
    if skill_id is not None:
        sid = skill_id.strip()
        if not _SKILL_ID_RE.fullmatch(sid):
            raise unprocessable(
                "Skill ID 只能包含小写字母、数字、连字符 - 和下划线 _，且以字母或数字开头（最长 48 字符）"
            )
        if sid in BUILTIN_SKILL_MAP:
            raise conflict(f"Skill ID「{sid}」与内置技能重名")
    if not skill.handlers:
        raise unprocessable("至少需要选择一个适用场景（chat 或 tldr）")
    if not skill.name.strip():
        raise unprocessable("名称不能为空")
    if not skill.description.strip():
        raise unprocessable("触发说明不能为空")
    if not skill.instructions.strip():
        raise unprocessable("技能正文不能为空")


# --- Tools Routes ---------------------------------------------------------------------


@router.get("/tools/ops")
async def get_ops_tools_overview() -> OpsToolsOverviewOut:
    """List all registered ops agent tools, global KOOK write whitelist, and global role authorization rules."""
    return await _load_ops_overview()


@router.put("/tools/ops")
async def update_ops_tools_config(body: OpsToolsConfigIn, actor: Actor) -> OpsToolsOverviewOut:
    """Update global ``ops.kook_write_actions`` and/or ``ops.role_rules``."""
    patch: dict[str, Any] = {}
    if body.kook_write_actions is not None:
        cleaned = list(dict.fromkeys("/" + p.strip().lstrip("/") for p in body.kook_write_actions if p.strip()))
        forbidden = [p for p in cleaned if p in _KOOK_NEVER]
        if forbidden:
            raise unprocessable(f"高危接口禁止加入白名单：{', '.join(forbidden)}")
        patch["kook_write_actions"] = cleaned
    if body.role_rules is not None:
        normalized_rules: dict[str, Any] = {}
        for rid, rule in body.role_rules.items():
            clean_rid = rid.strip()
            if not clean_rid:
                continue
            writes = list(dict.fromkeys("/" + p.strip().lstrip("/") for p in rule.kook_write_actions if p.strip()))
            forbidden = [p for p in writes if p in _KOOK_NEVER]
            if forbidden:
                raise unprocessable(f"角色 {clean_rid} 不能授权高危接口：{', '.join(forbidden)}")
            normalized_rules[clean_rid] = {
                "tools": list(dict.fromkeys(rule.tools)),
                "kook_write_actions": writes,
                "guild_only": rule.guild_only,
            }
        patch["role_rules"] = normalized_rules

    if patch:
        async with change(actor, "ops", "update", "global") as c:
            row = await c.session.get(SettingsDoc, "global")
            existing = dict(row.value) if row and isinstance(row.value, dict) else {}
            new_doc = _save_global_section_patch(existing, "ops", patch)
            if row is None:
                c.session.add(SettingsDoc(key="global", value=new_doc))
            else:
                row.value = new_doc
            c.detail = {"keys": list(patch.keys())}
    return await _load_ops_overview()


@router.get("/tools")
async def get_tools_overview() -> ToolsOverviewOut:
    """List all agent tools with their parameters, live readiness diagnostics, and masked standalone secrets."""
    return await _load_tools_overview()


@router.put("/tools/general")
async def update_general_tool_policy(body: GeneralToolPolicyIn, actor: Actor) -> ToolsOverviewOut:
    """Update shared tool execution limits (``request_limit``, ``tool_calls_limit``, ``slow_tools``)."""
    async with change(actor, "tools", "update_general", "global") as c:
        row = await c.session.get(SettingsDoc, "global")
        existing = dict(row.value) if row and isinstance(row.value, dict) else {}
        patch: dict[str, Any] = {}
        if body.request_limit is not None:
            patch["request_limit"] = body.request_limit
        if body.tool_calls_limit is not None:
            patch["tool_calls_limit"] = body.tool_calls_limit
        if body.slow_tools is not None:
            patch["slow_tools"] = list(dict.fromkeys(s.strip() for s in body.slow_tools if s.strip()))
        new_doc = _save_global_tools_patch(existing, patch)
        if row is None:
            c.session.add(SettingsDoc(key="global", value=new_doc))
        else:
            row.value = new_doc
        c.detail = {"patch": patch}
    return await _load_tools_overview()


@router.put("/tools/{tool_id}")
async def update_tool_config(tool_id: ToolName, body: ToolConfigPatchIn, actor: Actor) -> ToolsOverviewOut:
    """Update one tool's global default toggle, slow-tool flag, dedicated parameters, and optional encrypted secret."""
    spec = TOOL_SPEC_MAP.get(tool_id)
    if spec is None:
        raise not_found(f"工具 {tool_id}")

    allowed_keys = set(spec.param_keys)
    unknown = [k for k in body.params if k not in allowed_keys]
    if unknown:
        raise unprocessable(f"工具 {tool_id} 不支持参数：{', '.join(unknown)}")
    if body.secret is not None and not spec.supports_secret:
        raise unprocessable(f"工具 {tool_id} 不支持独立凭据配置")

    async with change(actor, "tools", "update", tool_id) as c:
        global_row = await c.session.get(SettingsDoc, "global")
        existing_global = dict(global_row.value) if global_row and isinstance(global_row.value, dict) else {}
        effective = merge_settings(existing_global)

        patch: dict[str, Any] = dict(body.params)
        if body.enabled is not None:
            cur_enabled = list(effective.tools.enabled)
            if body.enabled and tool_id not in cur_enabled:
                cur_enabled.append(tool_id)
            elif not body.enabled and tool_id in cur_enabled:
                cur_enabled.remove(tool_id)
            patch["enabled"] = cur_enabled
        if body.slow is not None:
            cur_slow = list(effective.tools.slow_tools)
            if body.slow and tool_id not in cur_slow:
                cur_slow.append(tool_id)
            elif not body.slow and tool_id in cur_slow:
                cur_slow.remove(tool_id)
            patch["slow_tools"] = cur_slow

        if patch:
            new_doc = _save_global_tools_patch(existing_global, patch)
            if global_row is None:
                c.session.add(SettingsDoc(key="global", value=new_doc))
            else:
                global_row.value = new_doc

        secret_audit: dict[str, Any] | None = None
        if body.secret is not None:
            sec_row = await c.session.get(SettingsDoc, TOOL_SECRETS_KEY)
            all_secrets = dict(sec_row.value) if sec_row and isinstance(sec_row.value, dict) else {}
            raw_prev = all_secrets.get(tool_id)
            prev: dict[str, Any] = dict(raw_prev) if isinstance(raw_prev, dict) else {}
            prev["enabled"] = body.secret.enabled
            prev["base_url"] = body.secret.base_url.strip().rstrip("/")
            prev["headers"] = {k.strip(): v for k, v in body.secret.headers.items() if k.strip()}
            if body.secret.clear_api_key:
                prev.pop("api_key_enc", None)
            elif body.secret.api_key and body.secret.api_key.strip():
                prev["api_key_enc"] = encrypt(body.secret.api_key.strip()).decode()
            all_secrets[tool_id] = prev
            if sec_row is None:
                c.session.add(SettingsDoc(key=TOOL_SECRETS_KEY, value=all_secrets))
            else:
                sec_row.value = all_secrets
            secret_audit = {
                "enabled": prev["enabled"],
                "base_url": prev["base_url"],
                "key_changed": bool(body.secret.api_key) or body.secret.clear_api_key,
            }

        c.detail = {"tool": tool_id, "params": list(patch.keys()), "secret": secret_audit}
    return await _load_tools_overview()


@router.post("/tools/azuriom/test")
async def test_azuriom_tool() -> AzuriomTestOut:
    """Test Ygg Bridge connectivity using the saved credentials and global timeout."""
    async with get_session() as s:
        global_row = await s.get(SettingsDoc, "global")
        secrets_row = await s.get(SettingsDoc, TOOL_SECRETS_KEY)

    global_val = global_row.value if global_row and isinstance(global_row.value, dict) else {}
    secrets_val = secrets_row.value if secrets_row and isinstance(secrets_row.value, dict) else {}
    sec_raw = secrets_val.get("azuriom_lookup")
    secret = _secret_info(sec_raw if isinstance(sec_raw, dict) else None)
    timeout = merge_settings(global_val).tools.azuriom_timeout_s
    started = time.perf_counter()
    try:
        result = await health_check(secret=secret, timeout_s=timeout)
    except AzuriomError as e:
        return AzuriomTestOut(ok=False, latency_ms=int((time.perf_counter() - started) * 1000), error=str(e))
    return AzuriomTestOut(
        ok=True,
        latency_ms=result["latency_ms"],
        endpoint=result["endpoint"],
        plugin=result["plugin"] if isinstance(result.get("plugin"), str) else "",
        version=result["version"] if isinstance(result.get("version"), str) else "",
        game=result["game"] if isinstance(result.get("game"), str) else None,
        kook_accounts_ready=result["kook_accounts_ready"]
        if isinstance(result.get("kook_accounts_ready"), bool)
        else None,
        kook_account_model=result["kook_account_model"] if isinstance(result.get("kook_account_model"), bool) else None,
    )


@router.post("/tools/check_provenance/test")
async def test_provenance_tool(body: ProvenanceTestIn) -> ProvenanceTestOut:
    """Test OpenAI Content Provenance connectivity using an optional base64 file or a tiny 1x1 PNG probe."""
    req = body
    async with get_session() as s:
        global_row = await s.get(SettingsDoc, "global")
        secrets_row = await s.get(SettingsDoc, TOOL_SECRETS_KEY)
        provider_rows = (await s.scalars(select(Provider))).all()

    global_val = dict(global_row.value) if global_row and isinstance(global_row.value, dict) else {}
    secrets_val = dict(secrets_row.value) if secrets_row and isinstance(secrets_row.value, dict) else {}
    effective = merge_settings(global_val)
    sec_raw = secrets_val.get("check_provenance")
    secret = _secret_info(sec_raw if isinstance(sec_raw, dict) else None)
    providers = {p.id: _provider_info(p) for p in provider_rows}

    # Resolve source label + endpoint against fresh DB state (without waiting for Redis reload).
    provider_ref = effective.tools.provenance_provider
    st, src_label, src_detail = _describe_provenance_from_db(provider_ref, secret, providers)
    src_info = describe_provenance_source(provider_ref, secret=secret)

    if req.file_b64:
        try:
            is_data_url = "," in req.file_b64 and req.file_b64.startswith("data:")
            raw_b64 = req.file_b64.split(",", 1)[1] if is_data_url else req.file_b64
            data = base64.b64decode(raw_b64, validate=True)
        except Exception as e:
            raise unprocessable(f"无法解码上传文件（base64 无效）：{e}") from e
        filename = req.filename.strip() or "upload.bin"
        mime = req.mime.strip()
    else:
        data = _PROBE_PNG
        filename = "probe-1x1.png"
        mime = "image/png"

    if st != "ready":
        return ProvenanceTestOut(
            ok=False,
            latency_ms=0,
            source=src_label,
            endpoint=src_info.endpoint,
            filename=filename,
            error=src_detail,
        )

    # Resolve fresh credentials from DB when using a Provider (so newly added providers work immediately).
    effective_secret = secret
    effective_provider_ref = provider_ref
    endpoint_hint = src_info.endpoint
    if provider_ref.strip():
        matched: ProviderInfo | None = None
        if provider_ref.strip().isdigit() and (cand := providers.get(int(provider_ref.strip()))) is not None:
            matched = cand
        else:
            ref_l = provider_ref.strip().lower()
            matched = next((p for p in providers.values() if p.enabled and p.name.strip().lower() == ref_l), None)
        if matched is not None and matched.api_key:
            effective_secret = ToolSecretInfo(
                enabled=True, base_url=matched.base_url, api_key=matched.api_key, headers=dict(matched.headers)
            )
            effective_provider_ref = ""
            endpoint_hint = matched.base_url
    elif not secret.enabled:
        auto_p = next(
            (
                p
                for p in providers.values()
                if p.enabled and p.api_format == "openai" and p.api_key and "api.openai.com" in p.base_url.lower()
            ),
            None,
        )
        if auto_p is not None and auto_p.api_key:
            effective_secret = ToolSecretInfo(
                enabled=True, base_url=auto_p.base_url, api_key=auto_p.api_key, headers=dict(auto_p.headers)
            )
            endpoint_hint = auto_p.base_url

    max_bytes = min(effective.tools.provenance_max_mb * 1024 * 1024, PROVENANCE_MAX_BYTES)
    started = time.perf_counter()
    try:
        report = await check_content_provenance(
            data,
            filename,
            mime=mime,
            provider_ref=effective_provider_ref,
            secret=effective_secret,
            timeout_s=float(effective.tools.provenance_timeout_s),
            max_bytes=max_bytes,
        )
    except ProvenanceError as e:
        return ProvenanceTestOut(
            ok=False,
            latency_ms=int((time.perf_counter() - started) * 1000),
            source=src_label,
            endpoint=endpoint_hint,
            filename=filename,
            error=str(e),
        )
    return ProvenanceTestOut(
        ok=True,
        latency_ms=int((time.perf_counter() - started) * 1000),
        source=src_label,
        endpoint=endpoint_hint,
        filename=filename,
        any_detected=report.any_detected,
        report=format_provenance_report(report, filename=filename),
    )


# --- Skills Routes --------------------------------------------------------------------


@router.get("/skills")
async def get_skills_overview() -> SkillsOverviewOut:
    """List built-in skills (with parameters & extra instructions) and all user-defined custom skills."""
    return await _load_skills_overview()


@router.put("/skills/builtin/{skill_id}")
async def update_builtin_skill(skill_id: str, body: BuiltinSkillPatchIn, actor: Actor) -> SkillsOverviewOut:
    """Update a built-in skill's default enabled flag, dedicated parameters, and optional extra instructions."""
    spec = BUILTIN_SKILL_MAP.get(skill_id)
    if spec is None:
        raise not_found(f"内置技能 {skill_id}")

    allowed_keys = set(spec.param_keys)
    unknown = [k for k in body.params if k not in allowed_keys]
    if unknown:
        raise unprocessable(f"内置技能 {skill_id} 不支持参数：{', '.join(unknown)}")

    async with change(actor, "skills", "update_builtin", skill_id) as c:
        global_row = await c.session.get(SettingsDoc, "global")
        existing_global = dict(global_row.value) if global_row and isinstance(global_row.value, dict) else {}
        effective = merge_settings(existing_global)

        patch: dict[str, Any] = dict(body.params)
        if body.enabled is not None:
            cur_skills = list(effective.tools.skills)
            if body.enabled and skill_id not in cur_skills:
                cur_skills.append(skill_id)
            elif not body.enabled and skill_id in cur_skills:
                cur_skills.remove(skill_id)
            patch["skills"] = cur_skills

        if patch:
            new_doc = _save_global_tools_patch(existing_global, patch)
            if global_row is None:
                c.session.add(SettingsDoc(key="global", value=new_doc))
            else:
                global_row.value = new_doc

        if body.extra_instructions is not None:
            skills_row = await c.session.get(SettingsDoc, SKILLS_CONFIG_KEY)
            skills_doc = dict(skills_row.value) if skills_row and isinstance(skills_row.value, dict) else {}
            overrides = dict(skills_doc.get("builtin_overrides") or {})
            cleaned = body.extra_instructions.strip()
            if cleaned:
                overrides[skill_id] = {"extra_instructions": cleaned}
            else:
                overrides.pop(skill_id, None)
            skills_doc["builtin_overrides"] = overrides
            if skills_row is None:
                c.session.add(SettingsDoc(key=SKILLS_CONFIG_KEY, value=skills_doc))
            else:
                skills_row.value = skills_doc

        extra_len = len(body.extra_instructions.strip()) if body.extra_instructions is not None else None
        c.detail = {
            "skill_id": skill_id,
            "params": list(patch.keys()),
            "extra_instructions_len": extra_len,
        }
    return await _load_skills_overview()


@router.post("/skills/custom")
async def create_custom_skill(body: CustomSkillIn, actor: Actor) -> CustomSkillOut:
    """Create a user-defined instruction skill (loaded on demand or eagerly as a pydantic-ai Capability)."""
    sid = body.id.strip()
    _validate_custom_skill(body, skill_id=sid)
    normalized = CustomSkillOut(
        id=sid,
        name=body.name.strip(),
        description=body.description.strip(),
        instructions=body.instructions.strip(),
        defer_loading=body.defer_loading,
        handlers=list(dict.fromkeys(body.handlers)),
        allow_private=body.allow_private,
        enabled=body.enabled,
        default_active=body.default_active,
    )

    async with change(actor, "skills", "create_custom", sid) as c:
        row = await c.session.get(SettingsDoc, SKILLS_CONFIG_KEY)
        doc = dict(row.value) if row and isinstance(row.value, dict) else {}
        items = [dict(x) for x in (doc.get("custom") or ()) if isinstance(x, dict)]
        if any(x.get("id") == sid for x in items):
            raise conflict(f"自定义技能「{sid}」已存在")
        items.append(normalized.model_dump(mode="json"))
        doc["custom"] = items
        if row is None:
            c.session.add(SettingsDoc(key=SKILLS_CONFIG_KEY, value=doc))
        else:
            row.value = doc
        c.detail = {"id": sid, "name": normalized.name, "defer_loading": normalized.defer_loading}
    return normalized


@router.put("/skills/custom/{skill_id}")
async def update_custom_skill(skill_id: str, body: CustomSkillUpdateIn, actor: Actor) -> CustomSkillOut:
    """Update an existing user-defined skill."""
    sid = skill_id.strip()
    _validate_custom_skill(body)
    updated = CustomSkillOut(
        id=sid,
        name=body.name.strip(),
        description=body.description.strip(),
        instructions=body.instructions.strip(),
        defer_loading=body.defer_loading,
        handlers=list(dict.fromkeys(body.handlers)),
        allow_private=body.allow_private,
        enabled=body.enabled,
        default_active=body.default_active,
    )

    async with change(actor, "skills", "update_custom", sid) as c:
        row = await c.session.get(SettingsDoc, SKILLS_CONFIG_KEY)
        doc = dict(row.value) if row and isinstance(row.value, dict) else {}
        items = [dict(x) for x in (doc.get("custom") or ()) if isinstance(x, dict)]
        idx = next((i for i, x in enumerate(items) if x.get("id") == sid), None)
        if idx is None:
            raise not_found(f"自定义技能 {sid}")
        items[idx] = updated.model_dump(mode="json")
        doc["custom"] = items
        if row is None:
            c.session.add(SettingsDoc(key=SKILLS_CONFIG_KEY, value=doc))
        else:
            row.value = doc
        c.detail = {"id": sid, "name": updated.name, "enabled": updated.enabled}
    return updated


@router.delete("/skills/custom/{skill_id}")
async def delete_custom_skill(skill_id: str, actor: Actor) -> Ok:
    """Delete a user-defined skill."""
    sid = skill_id.strip()
    async with change(actor, "skills", "delete_custom", sid) as c:
        row = await c.session.get(SettingsDoc, SKILLS_CONFIG_KEY)
        doc = dict(row.value) if row and isinstance(row.value, dict) else {}
        items = [dict(x) for x in (doc.get("custom") or ()) if isinstance(x, dict)]
        kept = [x for x in items if x.get("id") != sid]
        if len(kept) == len(items):
            raise not_found(f"自定义技能 {sid}")
        doc["custom"] = kept
        if row is not None:
            row.value = doc
        c.detail = {"id": sid}
    return Ok()
