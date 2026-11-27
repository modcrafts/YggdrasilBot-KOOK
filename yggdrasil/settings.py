"""Customizable behaviour, stored as sparse JSON overrides in the database.

Four-level inheritance:
``global`` -> ``guild`` -> ``category`` -> ``channel``.

Each level only stores fields explicitly overridden at that level; :func:`merge_settings` deep-merges the active
chain and validates once into :class:`Settings`. The dashboard renders forms directly from
``Settings.model_json_schema()``.
"""

from typing import Any, Literal, Self

from pydantic import BaseModel, Field, model_validator

ToolName = Literal[
    "read_url",
    "web_search",
    "read_x",
    "search_x",
    "read_file",
    "view_image",
    "check_provenance",
    "search_history",
    "get_messages_around",
    "recent_chat",
    "lookup_glossary",
    "get_profile",
    "render_mermaid",
    "render_markdown",
    "render_html",
    "generate_image",
    "sandbox",
]


class FeatureSettings(BaseModel):
    """Per-scope one-click feature toggles (inherited along global -> guild -> category -> channel)."""

    chat: bool = Field(default=True, description="是否响应对话问答（@提及、回复、点名）")
    tldr: bool = Field(default=True, description="是否响应 TL;DR / 总结指令")
    privacy_commands: bool = Field(default=True, description="是否响应「不要记我 / 记得我 / 忘记我」隐私指令")
    sticker_collect: bool = Field(default=True, description="是否收集本频道发出的表情包与服务器自定义表情")
    status_reactions: bool = Field(default=True, description="是否给触发消息贴状态表情回应（关闭可一键静默）")


class RoleAccessRule(BaseModel):
    """Per-KOOK-role permission override (keyed by KOOK role_id string)."""

    allow_bot: bool | None = Field(
        default=None,
        description="是否允许该角色触发 bot（true 放行 / false 禁止；不填或 null 表示沿用当前范围默认规则）",
    )
    tools_allow: list[ToolName] = Field(
        default_factory=list,
        description="为该角色额外开放的工具（在当前范围 tools.enabled 之外追加）",
    )
    tools_deny: list[ToolName] = Field(
        default_factory=list,
        description="禁止该角色使用的工具（优先级高于允许，可单独禁用 sandbox 或任意工具）",
    )
    bypass_channel_privacy: bool = Field(
        default=False,
        description="允许该角色在当前配置范围内绕过跨频道隐私与引用限制（可读取同服所有已启用频道；支持按服务器/分组/频道四级覆盖）",
    )


class AccessSettings(BaseModel):
    """Four-level (global -> guild -> category -> channel) bot & KOOK role access control.

    Superusers always bypass these restrictions.
    """

    allow_bot: bool = Field(
        default=True,
        description="当前范围是否允许成员触发 bot（关闭后仅超级管理员或显式放行的角色可用，频道消息照常记录）",
    )
    allowed_roles: list[str] = Field(
        default_factory=list,
        description="允许使用 bot 的 KOOK 角色 ID 白名单（留空表示不限制角色；填写后仅拥有任一指定角色的成员可用）",
    )
    denied_roles: list[str] = Field(
        default_factory=list,
        description="禁止使用 bot 的 KOOK 角色 ID 黑名单（拥有任一黑名单角色则不能触发 bot，优先于白名单）",
    )
    allow_guild_admins: bool = Field(
        default=True,
        description="配置了 allowed_roles 白名单时，是否仍然自动放行 KOOK 服务器主与管理员角色",
    )
    role_rules: dict[str, RoleAccessRule] = Field(
        default_factory=dict,
        description="按 KOOK 角色 ID 细粒度控制：单独覆盖该角色是否可用 bot 以及增删可用工具（键为角色 ID）",
    )


class PersonaSettings(BaseModel):
    name: str = Field(default="尤克特拉希尔", description="bot 的名字，出现在提示词里")
    aliases: list[str] = Field(
        default_factory=lambda: ["ygg", "世界树"],
        description="点名触发词（name_mention 规则使用；可随时按全局/服务器/分组/频道修改或清空）",
    )
    description: str = Field(
        default="一个懂行、靠谱、有点毒舌但心软的老朋友。说话轻松口语化，偶尔吐槽，但从不敷衍正经问题。",
        description="人设：性格、说话风格、口癖等，会写进所有对话类提示词",
    )


class InteractionSettings(BaseModel):
    # KOOK Unicode emoji status reactions (empty string disables an individual reaction).
    received_emoji: str = Field(default="👀", description="收到请求后立刻贴的表情")
    slow_emoji: str = Field(default="⏳", description="请求跑了 slow_after_s 秒还没有结果时贴的表情")
    slow_after_s: int = Field(default=30, ge=5, le=600, description="请求跑了多少秒还没有结果就贴 slow_emoji")
    silent_emoji: str = Field(default="🤫", description="bot 决定不回复时默认贴的表情（留空则不贴固定沉默表情）")
    silent_reaction_mode: Literal["auto", "fixed", "none"] = Field(
        default="auto",
        description=(
            "沉默（mode=silent）时的表情回应模式："
            "auto 允许模型在有必要时自选单个表情（没必要可留空，留空则回退到 silent_emoji；"
            "silent_emoji 也为空则完全无响应）；"
            "fixed 禁止模型自选，仅按 silent_emoji 处理；"
            "none 彻底静默，不提供自选也不贴任何沉默表情（仍会取消最初的 received_emoji）"
        ),
    )
    review_emoji: str = Field(default="❔", description="输入被拦截、输出进入审核或被拦截时贴的表情")
    action_emoji: str = Field(default="✅", description="按指令完成了操作（撤回、改配置等）且不需要回复时贴的表情")
    failure_emoji: str = Field(default="❌", description="出错时贴的表情")
    refusal_notice: bool = Field(
        default=True,
        description="模型因内容限制拒绝回答时，发一条消息说明原因（提供商给的分类和说明）；关掉则只贴 failure_emoji",
    )
    ratelimited_emoji: str = Field(default="🚦", description="被限流或排队超时时贴的表情")

    # Adaptive KOOK reply formatting.
    reply_style: Literal["auto", "card", "invisible", "kmarkdown"] = Field(
        default="auto",
        description="输出样式：auto 自适应（短回复用简洁 KMarkdown，普通对话长文/多节/带图升级为无边框 invisible 卡片，"
        "TL;DR/报告用标准有框卡片）；card 始终标准有框卡片；invisible 始终无边框卡片（TL;DR 仍用标准卡片）；"
        "kmarkdown 尽量用 KMarkdown",
    )
    adaptive_card_chars: int = Field(
        default=350,
        ge=80,
        le=3000,
        description="reply_style=auto 时，单节纯文字超过这个字数自动升级为卡片消息以提升阅读体验",
    )
    max_sections: int = Field(default=4, ge=1, le=10, description="一次回复最多几节，多出的节合并进最后一节")
    section_max_chars: int = Field(default=1500, ge=300, le=4500, description="卡片单个正文模块的最大字符数")
    tldr_max_chars: int = Field(
        default=200, ge=50, le=2000, description="tldr 正文的目标字数（模型通常会写超 1.5–2 倍）"
    )
    tldr_focus_max_chars: int = Field(
        default=150, ge=50, le=2000, description="tldr 后面带了关注点时，正文的目标字数（只回答关注点）"
    )
    quote_reply: Literal["always", "when_busy", "never"] = Field(
        default="always",
        description=(
            "回复时是否使用 KOOK 原生引用回复（quote）："
            "always 总是引用；when_busy 之后频道里已有别人发言才引用；never 从不"
        ),
    )
    metadata_node: bool = Field(default=True, description="卡片回复底部是否附带灰色元数据页脚（模型、耗时、token 等）")
    metadata_fields: list[Literal["model", "tokens", "tools", "latency", "moderation", "trace"]] = Field(
        default_factory=lambda: ["model", "tokens", "tools", "latency", "moderation", "trace"]
    )
    ignore_users: list[str] = Field(
        default_factory=list,
        description="不响应这些 KOOK 用户 ID：消息照常记录，但不会触发任何指令（超级管理员不受影响）",
    )
    allow_bot_triggers: bool = Field(
        default=True,
        description=(
            "是否允许其他 KOOK 机器人账号（author.bot=true 或昵称含 [bot]）触发本 bot（受下方防死循环保护限制）"
        ),
    )
    bot_cooldown_s: int = Field(
        default=20,
        ge=0,
        le=3600,
        description="其他 bot 触发的最小冷却秒数（从上次触发和上次回复完成起重新计时，防止高频互刷）",
    )
    bot_max_consecutive_turns: int = Field(
        default=2,
        ge=1,
        le=20,
        description=(
            "无人类发言穿插时，同一频道最多连续回应其他 bot 几次"
            "（超过即熔断，防止长文本 30s+ 慢速死循环；任一非 bot 用户发言后自动重置）"
        ),
    )
    bot_max_per_window: int = Field(
        default=3,
        ge=1,
        le=50,
        description="同一频道内单个其他 bot 在 bot_window_s 秒窗口内最多触发几次（兜底防慢速循环）",
    )
    bot_window_s: int = Field(
        default=600,
        ge=60,
        le=86400,
        description="bot_max_per_window 的统计时间窗口（秒）",
    )


class ModerationCategoryRule(BaseModel):
    threshold: float = Field(default=0.8, ge=0, le=1)
    action: Literal["pass", "review", "drop"] = "review"


class ModerationSettings(BaseModel):
    enabled: bool = True
    check_input: bool = True
    check_output: bool = True
    default_rule: ModerationCategoryRule = Field(default_factory=ModerationCategoryRule)
    category_rules: dict[str, ModerationCategoryRule] = Field(
        default_factory=lambda: {
            "Politically Sensitive Topics": ModerationCategoryRule(threshold=0.8, action="review"),
            "Sexual Content or Sexual Acts": ModerationCategoryRule(threshold=0.8, action="review"),
            "Jailbreak": ModerationCategoryRule(threshold=0.6, action="drop"),
        },
        description="按 Qwen3Guard 类别覆盖阈值和动作",
    )
    input_action_on_hit: Literal["drop", "pass"] = Field(default="drop", description="输入超阈值时：不响应或放行")
    notify_admins_on_input_block: bool = False
    notify_channel_on_review: bool = Field(default=True, description="进入审核时频道里先回一句（模型生成）")
    review_expire_minutes: int = Field(default=120, ge=5)
    disable_unsafe_stickers: bool = Field(
        default=False,
        description="表情包打标时被审核判为有风险的直接禁用；关闭时只做标记（全局设置生效）",
    )


class RateLimitSettings(BaseModel):
    user_per_minute: int = 4
    user_per_day: int = 80
    channel_per_minute: int = 20
    channel_daily_tokens: int = 3_000_000
    max_concurrent_per_channel: int = Field(default=4, ge=1, description="每个频道同时运行的请求数，超出排队")
    max_concurrent_per_user: int = Field(default=2, ge=1, description="每个人同时运行的请求数，超出排队")
    max_queued_per_user: int = Field(default=3, ge=0, description="每个人最多排队几个请求，再多就丢弃")
    queue_timeout_s: float = Field(default=180, gt=0, description="排队超过这个时间就放弃")
    repeat_window_s: int = Field(
        default=300,
        ge=0,
        description="同一目标的 tldr 在这段时间内只做一次，重复请求只贴 silent_emoji（超级管理员不受限；0 不去重）",
    )


class ContextSettings(BaseModel):
    recent_messages: int = Field(default=40, ge=0, le=300)
    recent_minutes: int = Field(default=30, ge=0)
    max_context_chars: int = Field(default=12000, ge=1000)
    include_profiles: bool = True
    include_glossary: bool = True
    include_open_topics: bool = True
    inline_images: int = Field(
        default=4,
        ge=0,
        le=10,
        description="被引用消息和触发消息里的图片，最多直接附几张给能看图的模型（0：改由模型调用 view_image）",
    )
    continue_minutes: int = Field(
        default=60,
        ge=0,
        description="回复 bot 回答时，这么多分钟内的回答续接原对话（0：关闭续接）",
    )


class ToolSettings(BaseModel):
    enabled: list[ToolName] = Field(
        default_factory=lambda: [
            "read_url",
            "web_search",
            "read_x",
            "search_x",
            "read_file",
            "view_image",
            "check_provenance",
            "search_history",
            "get_messages_around",
            "recent_chat",
            "lookup_glossary",
            "get_profile",
            "render_mermaid",
            "render_markdown",
            "render_html",
            "sandbox",
        ],
        description="启用的工具（按全局/服务器/分组/频道可勾选开关）；sandbox 表示整套工作区工具；"
        "generate_image 需配置 image 路由，默认关闭",
    )
    skills: list[str] = Field(
        default_factory=lambda: ["kook", "stickers"],
        description=(
            "按需加载的技能：kook 提供 KOOK 服务器/频道信息、置顶消息、成员资料、头像、表情回应与上传文件；"
            "stickers 允许回复时适度从已收集表情包库挑选内联表情或表情包图片"
        ),
    )
    slow_tools: list[str] = Field(
        default_factory=lambda: [
            "read_url",
            "web_search",
            "read_x",
            "search_x",
            "read_file",
            "view_image",
            "check_provenance",
            "render_mermaid",
            "render_markdown",
            "render_html",
            "generate_image",
            "sh",
            "py",
            "sandbox_put",
            "reset_workspace",
            "upload_file",
        ],
        description="耗时工具：回复末尾的元数据里显示它们的累计耗时",
    )
    read_url_max_chars: int = Field(default=16000, ge=1000, le=100_000, description="read_url / read_x 单页最大字符数")
    browser_fallback: bool = Field(
        default=True, description="read_url 遇到验证页或 JS 渲染页时自动切换浏览器抓取（需配置 YGG_BROWSER_URL）"
    )
    remote_fetch: bool = Field(
        default=True,
        description="read_url 在本地抓取和浏览器都读不到时，交给 DeepSeek 远程抓取",
    )
    file_max_mb: int = Field(default=30, ge=1, le=100, description="附件下载大小上限（MB）")
    file_max_pages: int = Field(default=40, ge=1, le=200, description="PDF 最大解析页数")
    file_max_chars: int = Field(default=60000, ge=2000, le=200_000, description="read_file 提取正文最大字符数")
    provenance_provider: str = Field(
        default="",
        description="OpenAI Content Provenance 检测（check_provenance）使用的提供商名称或 ID；"
        "留空时优先使用工具独立凭据，其次自动匹配控制台里指向 api.openai.com 的提供商，或回退到环境变量 OPENAI_API_KEY",
    )
    provenance_timeout_s: int = Field(default=30, ge=5, le=120, description="check_provenance 请求超时时间（秒）")
    provenance_max_mb: int = Field(
        default=25, ge=1, le=25, description="check_provenance 单文件上传大小上限（MB，OpenAI 上限 25MB）"
    )
    search_results: int = Field(default=6, ge=1, le=20, description="web_search / search_x 默认返回结果数")
    search_x_default_hours: int = Field(
        default=24, ge=1, le=720, description="search_x 未指定时间范围时的默认回溯小时数"
    )
    history_top_k: int = Field(default=12, ge=1, le=50, description="search_history 单次检索返回条数")
    messages_around_max: int = Field(
        default=40, ge=5, le=100, description="get_messages_around 前/后各自最多拉取的消息条数"
    )
    glossary_list_max: int = Field(
        default=200, ge=10, le=1000, description="lookup_glossary 列出全部词条时的最大返回数"
    )
    render_max_source_chars: int = Field(default=20000, ge=1000, le=100_000, description="render_* 输入源码最大字符数")
    render_timeout_s: int = Field(default=30, ge=5, le=120, description="render_* 渲染请求超时时间（秒）")
    render_markdown_max_pages: int = Field(default=4, ge=1, le=8, description="render_markdown 单次最多渲染页数")
    generate_image_max_references: int = Field(
        default=4, ge=0, le=10, description="generate_image 单次最多允许传入的参考图张数"
    )
    kook_upload_max_mb: int = Field(default=20, ge=1, le=50, description="kook 技能 upload_file 单文件上传上限（MB）")
    kook_pinned_shown: int = Field(default=5, ge=1, le=20, description="kook 技能 channel_info 展示置顶消息条数")
    kook_thread_body_chars: int = Field(
        default=6000, ge=1000, le=30_000, description="kook 技能 read_forum_thread 主楼正文最大字符数"
    )
    stickers_pool_limit: int = Field(
        default=200, ge=20, le=1000, description="stickers 技能 search_stickers 候选表情包检索池上限"
    )
    request_limit: int = Field(default=12, ge=1, le=100, description="无工作区工具时的单次对话模型调用上限")
    tool_calls_limit: int = Field(default=16, ge=1, le=200, description="无工作区工具时的单次对话工具调用上限")
    sandbox_enabled: bool = Field(
        default=True,
        description="工作区全局总开关；关掉后所有频道都没有工作区工具（单频道可在 tools.enabled 移出 sandbox）",
    )
    sandbox_command_timeout_s: int = Field(default=60, ge=1, le=300, description="工作区 sh、py 单次调用默认超时（秒）")
    sandbox_command_max_s: int = Field(default=300, ge=1, le=1800, description="工作区 sh、py 单次调用最长超时（秒）")
    sandbox_invocation_s: int = Field(default=600, ge=30, le=3600, description="一次请求工作区 sh、py 累计上限（秒）")
    sandbox_max_commands: int = Field(default=40, ge=1, le=200, description="一次请求最多调用多少次 sh、py")
    sandbox_idle_min: int = Field(default=30, ge=5, le=1440, description="工作区闲置多久（分钟）后回收")
    sandbox_max_age_min: int = Field(default=240, ge=10, le=10080, description="工作区最长存活时间（分钟）")
    sandbox_max_workspaces: int = Field(default=8, ge=1, le=50, description="全局同时存在的工作区上限")
    sandbox_max_workspaces_per_channel: int = Field(default=3, ge=1, le=10, description="每个频道最多同时有几个工作区")
    sandbox_max_links: int = Field(default=3, ge=0, le=20, description="每个工作区同时公开的链接上限")
    sandbox_request_limit: int = Field(default=40, ge=1, le=200, description="有工作区工具时的模型调用次数上限")
    sandbox_tool_calls_limit: int = Field(default=60, ge=1, le=400, description="有工作区工具时的工具调用次数上限")
    sandbox_max_jobs: int = Field(default=2, ge=0, le=10, description="每频道同时运行的后台任务上限（0 关闭后台任务）")
    sandbox_job_command_min: int = Field(default=30, ge=1, le=600, description="后台命令最长运行时间（分钟）")
    sandbox_delegate: bool = Field(default=True, description="后台大任务（delegate）开关")
    sandbox_job_agent_min: int = Field(default=10, ge=1, le=240, description="后台大任务最长运行时间（分钟）")
    sandbox_job_max_input_tokens: int = Field(default=3_000_000, ge=10_000)
    sandbox_job_max_output_tokens: int = Field(default=100_000, ge=1_000)
    bridge_run_limit: int = Field(default=30, ge=0, le=500)
    bridge_job_limit: int = Field(default=120, ge=0, le=2000)

    @model_validator(mode="after")
    def _sandbox_timeouts(self) -> Self:
        if self.sandbox_idle_min * 60 <= self.sandbox_invocation_s:
            raise ValueError("sandbox_idle_min 换算成秒必须大于 sandbox_invocation_s")
        if self.sandbox_command_timeout_s > self.sandbox_command_max_s:
            raise ValueError("sandbox_command_timeout_s 不能大于 sandbox_command_max_s")
        return self


OpsToolName = Literal[
    "status",
    "get_settings",
    "settings_reference",
    "list_channels",
    "list_routes",
    "list_triggers",
    "list_reviews",
    "recent_invocations",
    "list_events",
    "kook_api",
    "update_settings",
    "set_channel",
    "set_guild",
    "sync_kook",
    "backfill_channel",
    "decide_review",
    "save_trigger",
    "delete_trigger",
    "set_route",
    "undo_last_change",
    "recall_message",
]


class OpsRoleRule(BaseModel):
    """Per-KOOK-role authorization for the ops agent (keyed by KOOK role_id string)."""

    tools: list[OpsToolName] = Field(
        default_factory=list,
        description="允许该 KOOK 角色通过管理助手（ops）调用的工具子集",
    )
    kook_write_actions: list[str] = Field(
        default_factory=list,
        description=(
            "允许该角色通过 kook_api 调用的 KOOK 写接口路径"
            "（需同时授权 kook_api，且受全局 ops.kook_write_actions 与高危禁区限制）"
        ),
    )
    guild_only: bool = Field(
        default=True,
        description=(
            "限制该角色仅能查询/操作当前所在服务器内的频道、设置与消息，禁止修改全局配置、其他服务器或提升自身 ops 权限"
        ),
    )


class OpsSettings(BaseModel):
    kook_write_actions: list[str] = Field(
        default_factory=lambda: [
            "/message/create",
            "/message/delete",
            "/message/pin",
            "/message/unpin",
            "/direct-message/create",
            "/direct-message/delete",
            "/guild/nickname",
            "/guild/kickout",
            "/guild-mute/create",
            "/guild-mute/delete",
            "/channel/update",
        ],
        description="管理助手（超级管理员）通过 kook_api 可以调用的 KOOK 写接口路径；GET 只读接口默认放行。"
        "退服（/guild/leave）与删服等高危接口始终禁止",
    )
    role_rules: dict[str, OpsRoleRule] = Field(
        default_factory=dict,
        description=(
            "按 KOOK 角色 ID 授权调用部分管理助手（ops）工具"
            "（支持按全局/服务器/分组/频道四级覆盖；超级管理员始终拥有全部权限）"
        ),
    )


class MemorySettings(BaseModel):
    enabled: bool = Field(default=True, description="是否开启记忆流水线（切块、话题、画像、黑话、摘要）")
    share_in_guild: bool = Field(
        default=True,
        description="对外共享（单向隐私）：关闭后，其他频道默认无法引用本频道内容，且本频道不进入全服每日摘要/黑话/画像；"
        "本频道自身仍可引用公共频道或获授权频道（如需完全不读外部频道，请关闭 cross_channel_read）",
    )
    cross_channel_read: bool = Field(
        default=True,
        description="在当前频道聊天时是否允许引用同服其他频道的历史与话题（关闭后仅限引用当前频道自身）",
    )
    readable_by_scopes: list[str] = Field(
        default_factory=list,
        description=(
            "定向放行来源（频道 ID 或分组 ID）：即使 share_in_guild=false，"
            "也允许这些频道或分组引用本频道内容（例如工单分组放行给管理员频道/分组）"
        ),
    )
    readable_by_roles: list[str] = Field(
        default_factory=list,
        description=(
            "定向放行角色（KOOK 角色 ID）：即使 share_in_guild=false，拥有这些角色的成员触发 bot 时仍可读取本频道内容"
        ),
    )
    cross_channel_allow: list[str] = Field(
        default_factory=list,
        description=(
            "本频道可引用的目标白名单（频道 ID 或分组 ID；留空表示不限，"
            "仅受各频道共享/隐私规则约束；当前频道自身始终可见）"
        ),
    )
    cross_channel_deny: list[str] = Field(
        default_factory=list,
        description="本频道禁止引用的目标黑名单（频道 ID 或分组 ID；优先级高于白名单和公共共享）",
    )
    chunk_gap_minutes: int = Field(default=20, description="消息间隔超过这个时间就切块")
    chunk_max_messages: int = 60
    chunk_max_chars: int = 2400
    topic_min_new_messages: int = Field(default=120, description="新增消息达到这个数量触发话题分段")
    topic_window_messages: int = 400
    topic_close_after_minutes: int = 120
    topic_archive_below_messages: int = Field(default=20, ge=0)
    topic_archive_below_participants: int = Field(default=3, ge=0)
    topic_archive_below_weight: float = Field(default=3.0, ge=0)
    digest_hour: int = Field(default=4, ge=0, le=23, description="每日摘要生成时间（Asia/Shanghai）")
    profile_min_messages: int = 15
    fact_archive_below_importance: int = Field(default=3, ge=0)
    facts_per_user: int = Field(default=40, ge=0)
    weight_half_life_days: int = Field(default=180, ge=1)
    glossary_min_count: int = 6
    glossary_max_candidates: int = 30
    backfill_messages: int = Field(default=3000, description="频道回填的历史消息数上限")


class StickerDropSettings(BaseModel):
    enabled: bool = Field(default=False, description="频道聊得够多时，有机会挑一张收集到的表情包发到频道里")
    min_new_messages: int = Field(default=60, ge=1)
    daily_max: int = Field(default=20, ge=0)
    min_gap_minutes: int = Field(default=15, ge=0)
    active_within_minutes: int = Field(default=5, ge=1)
    min_fit: float = Field(default=0.4, ge=0, le=1)
    chance: float = Field(default=0.7, ge=0, le=1)
    candidates: int = Field(default=50, ge=2, le=255)
    guild_only: bool = Field(
        default=False,
        description="只从本服务器发过的表情里挑，按本服务器热度加权；关闭时用所有服务器收集到的表情",
    )
    context_messages: int = Field(default=30, ge=5, le=100)


class PrivacySettings(BaseModel):
    log_message_content: bool = False
    capture_llm_content: bool = Field(
        default=True,
        description="保存提示词、思考、工具参数和结果、图片原文：控制台的对话记录和 trace 都用它；关闭后只记结构和用量",
    )


class EventSettings(BaseModel):
    """Guild/channel system event policy (global document)."""

    disable_channel_when_deleted: bool = Field(
        default=True, description="KOOK 频道被删除时自动删除该频道配置（不删历史消息与记忆）"
    )
    disable_guild_when_exited: bool = Field(default=True, description="bot 退出或被移出服务器时自动停用其下频道")
    notify_guild_changes: bool = Field(default=True, description="bot 加入/退出服务器或被移出时私聊通知超级管理员")
    retention_days: int = Field(default=90, ge=7, description="事件记录保留天数")


class Settings(BaseModel):
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    access: AccessSettings = Field(default_factory=AccessSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    interaction: InteractionSettings = Field(default_factory=InteractionSettings)
    moderation: ModerationSettings = Field(default_factory=ModerationSettings)
    ratelimit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    context: ContextSettings = Field(default_factory=ContextSettings)
    tools: ToolSettings = Field(default_factory=ToolSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    privacy: PrivacySettings = Field(default_factory=PrivacySettings)
    ops: OpsSettings = Field(default_factory=OpsSettings)
    events: EventSettings = Field(default_factory=EventSettings)
    sticker_drop: StickerDropSettings = Field(default_factory=StickerDropSettings)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def merge_settings(*layers: dict[str, Any] | None) -> Settings:
    """Merge arbitrary sparse override layers (``global -> guild -> category -> channel``) into :class:`Settings`."""
    doc: dict[str, Any] = Settings().model_dump(mode="json")
    for layer in layers:
        if layer:
            doc = deep_merge(doc, layer)
    return Settings.model_validate(doc)


def effective_settings(*layers: dict[str, Any] | None) -> Settings:
    """Alias for :func:`merge_settings`."""
    return merge_settings(*layers)
