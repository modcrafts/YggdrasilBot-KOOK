# Tools 与 Skills 扩展开发指南

本文档说明 YggdrasilBot-KOOK 中 **工具（Tools）** 与 **技能（Skills）** 的配置架构，以及后续新增工具、新增代码型内置 Skill 或编写零代码自定义 Skill 的完整步骤。

---

## 1. 核心架构与职责划分

系统将 **工具/技能自身设置** 与 **范围权限管理** 清晰分离：

| 维度 | 存储位置 | 控制台入口 | 职责说明 |
| :--- | :--- | :--- | :--- |
| **工具与内置技能的非敏感参数** | `SettingsDoc("global")`（`Settings.tools`）及四级稀疏覆盖 | **工具与技能** (`/tools`) | 超时、页数/字数上限、默认开关、`slow_tools`、`request_limit` 等；自动支持 `global → guild → category → channel` 四级继承 |
| **工具独立加密凭据** | `SettingsDoc("tool.secrets")` | **工具与技能** (`/tools`) | 仅限工具自身的独立 API Key / Base URL / Headers（例如 `check_provenance`），使用 `YGG_SECRET_KEY`（Fernet）加密存储，API 响应仅返回末四位掩码 |
| **自定义 Skills 与内置 Skill 扩展指令** | `SettingsDoc("skills.config")` | **工具与技能 → Skills 技能** (`/tools`) | 用户创建的纯指令型 Skill，以及内置 Skill（`kook`/`stickers`）的 `extra_instructions` |
| **范围与角色权限管理** | `guilds.settings` / `channels.settings`（`access.*`、`tools.enabled`、`tools.skills`） | **服务器与频道** (`/channels`) | 决定哪些服务器、分组、频道或 KOOK 角色允许/禁止使用特定工具或技能 |

所有配置改动均通过 `yggdrasil.admin.common.change()` 事务写入 PostgreSQL 与审计表 `config_audit`，随后发布 Redis `ygg:config` 事件，全进程自动调用 `yggdrasil.runtime.load()` 刷新内存快照 `runtime.snapshot()`。

---

## 2. 如何新增一个内置工具（Tool）

适合需要执行外部请求、查询数据库或操作媒体的原子能力。由于控制台 `/tools` 页面已经采用 **后端元数据驱动（`param_fields`）+ 通用加密凭据表单（`supports_secret`）**，新增常规工具**无需修改前端页面代码**，按以下 **5 步** 即可完成：

### 第 1 步：实现底层逻辑（`yggdrasil/tools/<name>.py`）

- 面向外部 URL 抓取的工具必须先经过 `yggdrasil.tools.ssrf.check_url()` 防 SSRF。
- 可恢复的业务错误请抛出 `yggdrasil.tools.errors.ToolError`（`@chat_tool` 会自动把错误文本返回给模型而不是中断整轮对话）。
- 如果工具需要**独立加密凭据**，从 `yggdrasil.runtime.snapshot().tool_secrets.get("<tool_id>")` 读取 `ToolSecretInfo`：

```python
from yggdrasil.runtime import snapshot
from yggdrasil.tools.errors import ToolError


def _resolve_secret() -> tuple[str, str]:
    sec = snapshot().tool_secrets.get("my_tool")
    if sec is None or not sec.enabled or not sec.api_key:
        raise ToolError("my_tool 尚未配置独立 API Key，请在控制台「工具与技能」中配置")
    return sec.base_url or "https://api.example.com/v1", sec.api_key
```

### 第 2 步：封装为 Agent 工具并注册

1. 在 `yggdrasil/agent/tools/` 对应模块（或新建模块）中用 `@chat_tool("<tool_id>")` 装饰函数，从 `ctx.deps.settings.tools` 读取工具参数：

```python
from pydantic_ai import RunContext
from yggdrasil.agent.deps import Deps
from yggdrasil.agent.tools.base import chat_tool


@chat_tool("my_tool")
async def my_tool(ctx: RunContext[Deps], query: str) -> str:
    """一句话写清楚工具做什么、参数含义与何时调用。"""
    cfg = ctx.deps.settings.tools
    timeout = cfg.my_tool_timeout_s
    ...
```

2. 在 [yggdrasil/agent/tools/__init__.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/agent/tools/__init__.py) 将函数加入 `ALL_TOOLS`；如果该工具不适合在 `tldr` 场景使用，同时加入 `_HANDLER_EXCLUDED["tldr"]`。

### 第 3 步：声明工具名称与配置参数（`yggdrasil/settings.py`）

在 [yggdrasil/settings.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/settings.py) 中：

1. 将 `"my_tool"` 加入 `ToolName = Literal[...]`。
2. 决定是否默认开启：
   - 若面向所有成员默认开启，加入 `ToolSettings.enabled` 默认列表；
   - 若希望 **仅允许部分 KOOK 角色调用**，则**不要**加入默认 `ToolSettings.enabled`（保持全局默认关闭），之后在控制台 **「服务器与频道」→ 覆盖 → `access.role_rules` 的 `tools_allow`** 中按角色放行即可。
3. 在 `ToolSettings` 上新增该工具的专属参数字段（支持 `int` / `float` / `bool` / `str` / `Literal[...]`，务必设置合理的 `default`、`ge`/`le` 边界与中文 `description`）：

```python
my_tool_timeout_s: int = Field(default=15, ge=3, le=120, description="my_tool 请求超时（秒）")
my_tool_max_items: int = Field(default=5, ge=1, le=20, description="my_tool 单次返回条数上限")
```

> **提示**：前端会自动根据 `ToolSettings` 的 Pydantic Schema 渲染参数表单与校验范围；如需更友好的中文标签、单位或分组，可选在 `tools_api.py` 的 `_PARAM_UI_META` 中加一行即可。

### 第 4 步：注册到后台管理 API（`yggdrasil/admin/tools_api.py`）

在 [yggdrasil/admin/tools_api.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/admin/tools_api.py) 的 `TOOL_SPECS` 中追加一条 `ToolSpec`：

```python
(
    ToolSpec(
        id="my_tool",
        name="我的新工具",
        category="web",  # web | media | memory | render | sandbox
        description="简明说明工具用途与效果。",
        handlers=("chat", "tldr"),
        param_keys=("my_tool_timeout_s", "my_tool_max_items"),
        supports_secret=True,  # 开启后前端自动展示 Base URL + 加密 API Key + Headers 表单
        secret_default_base_url="https://api.example.com/v1",
        provider_param_key=None,  # 若同时支持复用控制台 Providers，填对应参数名
        related_route=None,
        related_label=None,
    ),
)
```

可选：在 `_diagnose_tool()` 中补充定制状态诊断（若工具设置了 `supports_secret=True`，默认已内置独立凭据就绪诊断）。

### 第 5 步：同步 OpenAPI、生成 SDK 并测试

```bash
YGG_SECRET_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= uv run python -m yggdrasil.admin.dump_openapi > web/openapi.json
cd web && npm run gen && npm run build
```

---

## 3. 如何拓展管理助手（`ops`）工具并按角色授权

管理助手运行在隔离的 `ops` Agent 中（不混入普通聊天上下文和网页内容，避免 Prompt Injection），支持**超级管理员全权调用**以及**按 KOOK 角色授权调用部分 `ops` 工具**。

### 新增一个 `ops` 工具（仅需 2 个文件）

1. **在 [yggdrasil/settings.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/settings.py)** 将新工具名加入 `OpsToolName = Literal[...]`。
2. **在 [yggdrasil/ops/tools.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/ops/tools.py)** 实现工具函数并注册到 `OPS_TOOL_SPECS`：

```python
@_op("my_ops_action", write=True)
async def my_ops_action(ctx: RunContext[OpsDeps], channel_id: str) -> str:
    """对当前服务器某个频道执行管理动作。"""
    # 重要：非超管默认开启 guild_only=True，写工具必须做同服作用域校验！
    _assert_channel_in_guild(ctx.deps, channel_id)
    ...
```

并在 `OPS_TOOL_SPECS` 中追加：

```python
OpsToolSpec("my_ops_action", "自定义管理动作", "config", True, False, "工具说明。", my_ops_action)
```

> - 若工具影响全局（如改全局路由、全量同步），将 `global_only=True` 并在函数开头调用 `_assert_not_guild_only(ctx.deps, "my_ops_action")`。
> - 注册进 `OPS_TOOL_SPECS` 并重新导出 OpenAPI 后，该工具会自动出现在控制台 **「工具与技能 → 管理助手 (ops)」** 的工具一览与角色授权勾选框中，**前端零改动**。

### 按 KOOK 角色开放部分 `ops` 工具

在控制台 **「工具与技能 → 管理助手 (ops)」**（或在 **「服务器与频道」** 对应服务器/分组/频道覆盖的 `ops.role_rules`）：
1. 选择「全局默认规则」或指定某个服务器（可自动拉取本服 KOOK 角色名）。
2. 点击 **「添加角色授权」**，勾选允许该角色调用的 `ops` 工具子集。
3. 若勾选了 `kook_api`，可进一步只勾选允许该角色调用的 `kook_write_actions` 子集（如仅放行 `/message/pin`、`/message/unpin`；不勾选则该角色仅能 `GET` 只读查询）。
4. 保持开启 **「仅限操作当前所在服务器（`guild_only`）」**：代码级强制限制该角色无法跨服操作、无法修改全局配置、无法修改 `ops` 权限本身或 `admin.*` 触发器。
5. 被授权角色成员在频道内发送 `@bot ops <指令>` 即可使用已授权的工具。

---

## 4. 如何新增一个内置 Skill（代码型工具包）

### 何时做成 Skill，而不是顶层 Tool？

- **适合顶层 Tool**：通用高频能力（如联网搜索、读链接、看图、查历史），模型几乎每轮都可能用到。
- **适合内置 Skill**：包含**一组强相关子工具 + 较长领域指令**的能力（如 `kook`、`stickers`）。设置 `defer_loading=True` 后，模型平时只看到一句 `DESCRIPTION`，不占用系统提示词 Token；真正需要时通过 `load_capability` 按需加载完整 `INSTRUCTIONS` 与子工具。

### 开发步骤

1. **新建模块 `yggdrasil/agent/skills/<skill_id>.py`**：
   - 定义 `ID`、`DESCRIPTION`、`INSTRUCTIONS` 和 `@chat_tool(...)` 子工具。
   - 导出 `capability(*, extra_instructions: str = "") -> Capability[Deps]`：

```python
from pydantic_ai import Tool
from pydantic_ai.capabilities import Capability
from yggdrasil.agent.deps import Deps

ID = "my_skill"
DESCRIPTION = "写清楚模型在什么场景下应该调用 load_capability 加载本技能。"
INSTRUCTIONS = """\
详细的工具使用规范与注意事项...
"""


def capability(*, extra_instructions: str = "") -> Capability[Deps]:
    text = INSTRUCTIONS if not extra_instructions.strip() else f"{INSTRUCTIONS}\n\n{extra_instructions.strip()}"
    return Capability[Deps](
        id=ID,
        description=DESCRIPTION,
        instructions=text,
        tools=[Tool(my_sub_tool, takes_ctx=True)],
        defer_loading=True,
    )
```

2. **注册到技能表**：
   - 在 [yggdrasil/agent/skills/__init__.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/agent/skills/__init__.py) 将 `my_skill.ID: my_skill.capability` 加入 `SKILLS`。
   - 若希望默认启用，加入 `ToolSettings.skills` 默认列表（`yggdrasil/settings.py`）。
   - 若技能有专属可调参数，同样添加到 `ToolSettings`（命名建议 `<skill_id>_<param>`）。

3. **暴露到控制台「Skills 技能」页面**：
   - 在 `yggdrasil/admin/tools_api.py` 的 `BUILTIN_SKILL_SPECS` 追加一条 `BuiltinSkillSpec`（包含子工具名列表和 `param_keys`）。
   - 在 `web/src/pages/tools.tsx` 的 `BUILTIN_SKILL_PARAM_SPECS` 补充参数表单项。
   - 重新导出 OpenAPI 并运行 `npm run gen`。

---

## 5. 如何新增自定义 Skill（零代码 / `SKILL.md`）

如果只需要注入**领域知识、社区规则、回答模板、排查 SOP 或工作流指引**（无需新增 Python 代码工具），直接在控制台 **「工具与技能 → Skills 技能 → 新建 Skill / 导入 SKILL.md」** 创建即可，实时热更新生效。

### 标准 `SKILL.md` 导入格式

```markdown
---
name: server-faq
description: 当群友询问服务器规则、活动时间、新人入服流程或常见问题时加载本技能。
---

# 服务器常见问题与回答指引

1. 涉及版规争议时先客观引用以下条款，不要替管理员做封禁承诺：
   - 禁止刷屏与广告...
2. 活动时间以每周末晚 20:00 为准...
```

### 关键配置项说明

- **`defer_loading`（按需加载）**：
  - `true`（推荐）：仅把 `description` 暴露给模型，节省 Token 且保持 prompt cache 命中率；相关时模型自动加载正文。
  - `false`（常驻注入）：每次符合条件的对话直接把正文注入系统指令，适合简短且每轮都必须遵守的规则。
- **`default_active`（全局默认生效）**：
  - `true`：启用后所有匹配场景的频道自动生效，无需逐个频道配置。
  - `false`：仅在 **「服务器与频道」** 的 `tools.skills` 覆盖项里显式添加该 Skill ID 的范围才会加载。
- **ID 约束**：仅限小写字母、数字、`-`、`_`，以字母或数字开头（最长 48 字符），不能包含冒号 `:`，且不可与内置技能（`kook`、`stickers`）重名。

---

## 6. 安全与质量检查清单

每次新增工具或内置 Skill 后，建议逐项检查：

- [ ] **不泄露密钥**：API Key / Token 只保存在 `SettingsDoc("tool.secrets")`（经 `yggdrasil.crypto.encrypt` 加密），绝不写入 `SettingsDoc("global")` 或 `ConfigAudit.detail` 明文。
- [ ] **出网安全**：读取用户提供 URL 的工具务必调用 `yggdrasil.tools.ssrf.check_url`。
- [ ] **无破坏性迁移**：优先复用 `ToolSettings` / `tool.secrets` / `skills.config`，确保旧配置兼容与四级覆盖正常。
- [ ] **OpenAPI 与前端同步**：执行完整校验：

```bash
# 1. 全仓库代码格式、Lint 与静态类型检查（与 CI 保持一致，含 tests/ 与 .md 中的 Python 代码块）
uv run ruff format --check .
uv run ruff check .
uv run basedpyright

# 2. 后端测试（含 OpenAPI 一致性检查）
YGG_SECRET_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= .venv/bin/pytest -q

# 3. 前端类型检查与生产构建
cd web && npx tsc -b --noEmit && npm run build
```
