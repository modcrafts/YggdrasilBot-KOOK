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

适合需要执行外部请求、查询数据库或操作媒体的原子能力。按以下 **7 步** 完成接入：

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
2. 决定是否默认开启：若默认开启，加入 `ToolSettings.enabled` 默认列表；若属于较慢的外部 I/O 工具，加入 `ToolSettings.slow_tools` 默认列表。
3. 在 `ToolSettings` 上新增该工具的专属参数字段（务必设置合理的 `default`、`ge`/`le` 边界与中文 `description`）：

```python
my_tool_timeout_s: int = Field(default=15, ge=3, le=120, description="my_tool 请求超时（秒）")
my_tool_max_items: int = Field(default=5, ge=1, le=20, description="my_tool 单次返回条数上限")
```

> **提示**：新增到 `ToolSettings` 的字段会自动进入 `Settings.model_json_schema()`，因此「服务器与频道」的四级稀疏覆盖无需任何额外代码即可支持该参数。

### 第 4 步：注册到后台管理 API（`yggdrasil/admin/tools_api.py`）

在 [yggdrasil/admin/tools_api.py](file:///home/user/repo/YggdrasilBot-KOOK/yggdrasil/admin/tools_api.py) 中：

1. 在 `TOOL_SPECS` 中追加一条 `ToolSpec`：

```python
ToolSpec(
    id="my_tool",
    name="我的新工具",
    category="web",  # web | media | memory | render | sandbox
    description="简明说明工具用途与效果。",
    handlers=("chat", "tldr"),
    param_keys=("my_tool_timeout_s", "my_tool_max_items"),
    supports_secret=False,  # 如需独立加密凭据设为 True
    related_route=None,     # 如依赖其他页面可填 "/providers" | "/routes" | "/search"
    related_label=None,
),
```

2. 在 `_diagnose_tool()` 中补充该工具的实时状态诊断分支（返回 `(status, status_label, status_detail)`），让控制台卡片准确显示「已就绪 / 需注意 / 未配置」。
3. 若需要在线测试功能（参考 `POST /tools/check_provenance/test`），在 `tools_api.py` 中新增测试端点（优先使用 JSON 入参，避免引入额外表单依赖）。

### 第 5 步：重新导出 OpenAPI 与前端 TypeScript SDK

```bash
YGG_SECRET_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= .venv/bin/python -m yggdrasil.admin.dump_openapi > web/openapi.json
cd web && npm run gen
```

> `tests/admin/test_admin_openapi.py` 会校验 `web/openapi.json` 与后端路由是否一致，改动任何 Admin API 后必须重新生成。

### 第 6 步：补充前端参数表单定义（`web/src/pages/tools.tsx`）

在 [web/src/pages/tools.tsx](file:///home/user/repo/YggdrasilBot-KOOK/web/src/pages/tools.tsx) 中：

1. 在 `TOOL_PARAM_SPECS` 中添加该工具的表单项（`int` 或 `bool`）：

```ts
my_tool: [
  { key: "my_tool_timeout_s", label: "请求超时", type: "int", unit: "秒", min: 3, max: 120 },
  { key: "my_tool_max_items", label: "最大返回条数", type: "int", unit: "条", min: 1, max: 20 },
],
```

2. 在 `summarizeParams()` 中补一行卡片参数摘要徽章。
3. 如果工具启用了 `supports_secret=True` 或需要专用测试弹窗，可参考 `check_provenance` 的 `ToolConfigDialog` / `ProvenanceTestDialog` 扩展。

### 第 7 步：编写测试与验证

- 在 `tests/tools/` 编写单元测试覆盖核心逻辑与参数边界。
- 在 `tests/admin/test_admin_tools.py` 验证 `GET /api/tools` 状态诊断与 `PUT /api/tools/<tool_id>` 更新。
- 运行全套校验命令（见文末第 5 节）。

---

## 3. 如何新增一个内置 Skill（代码型工具包）

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

## 4. 如何新增自定义 Skill（零代码 / `SKILL.md`）

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

## 5. 安全与质量检查清单

每次新增工具或内置 Skill 后，建议逐项检查：

- [ ] **不泄露密钥**：API Key / Token 只保存在 `SettingsDoc("tool.secrets")`（经 `yggdrasil.crypto.encrypt` 加密），绝不写入 `SettingsDoc("global")` 或 `ConfigAudit.detail` 明文。
- [ ] **出网安全**：读取用户提供 URL 的工具务必调用 `yggdrasil.tools.ssrf.check_url`。
- [ ] **无破坏性迁移**：优先复用 `ToolSettings` / `tool.secrets` / `skills.config`，确保旧配置兼容与四级覆盖正常。
- [ ] **OpenAPI 与前端同步**：执行完整校验：

```bash
# 1. 代码格式与静态类型检查
.venv/bin/ruff format --check yggdrasil
.venv/bin/ruff check yggdrasil
.venv/bin/basedpyright yggdrasil

# 2. 后端测试（含 OpenAPI 一致性检查）
YGG_SECRET_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= .venv/bin/pytest -q

# 3. 前端类型检查与生产构建
cd web && npx tsc -b --noEmit && npm run build
```
