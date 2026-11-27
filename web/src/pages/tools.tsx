import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  ArrowUpRightIcon,
  CheckCircle2Icon,
  FileUpIcon,
  KeyRoundIcon,
  PencilIcon,
  PlusIcon,
  RotateCcwIcon,
  SearchIcon,
  Settings2Icon,
  SparklesIcon,
  UploadIcon,
  WrenchIcon,
  ZapIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  createCustomSkillMutation,
  deleteCustomSkillMutation,
  getSkillsOverviewOptions,
  getToolsOverviewOptions,
  listProvidersOptions,
  testProvenanceToolMutation,
  updateBuiltinSkillMutation,
  updateCustomSkillMutation,
  updateGeneralToolPolicyMutation,
  updateToolConfigMutation,
} from "@/client/@tanstack/react-query.gen";
import type {
  BuiltinSkillOut,
  CustomSkillOut,
  ProviderOut,
  ToolItemOut,
  ToolSecretIn,
} from "@/client/types.gen";
import {
  ApiKeyField,
  ConfirmButton,
  Empty,
  Field,
  FilterBar,
  FormDialog,
  JsonEditor,
  Meta,
  MultiToggle,
  NumberInput,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
  SectionHeader,
} from "@/components/common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { isPlainObject } from "@/lib/format";
import { cn } from "@/lib/utils";

type ToolCategory = ToolItemOut["category"];
type ToolStatus = ToolItemOut["status"];

const CATEGORY_META: { value: ToolCategory | "all"; label: string }[] = [
  { value: "all", label: "全部" },
  { value: "web", label: "联网与搜索" },
  { value: "media", label: "媒体与内容检测" },
  { value: "memory", label: "历史与记忆" },
  { value: "render", label: "渲染与画图" },
  { value: "sandbox", label: "工作区套件" },
];

const CATEGORY_LABELS: Record<string, string> = Object.fromEntries(
  CATEGORY_META.filter((c) => c.value !== "all").map((c) => [c.value, c.label]),
);

type ParamFieldSpec = {
  key: string;
  label: string;
  hint?: string;
  type: "int" | "bool";
  unit?: string;
  min?: number;
  max?: number;
};

const TOOL_PARAM_SPECS: Record<string, ParamFieldSpec[]> = {
  read_url: [
    { key: "read_url_max_chars", label: "单页最大字符数", hint: "与 read_x 共用，超出后自动分页", type: "int", unit: "字", min: 1000, max: 100000 },
    { key: "browser_fallback", label: "浏览器渲染回退", hint: "遇到反爬/JS 渲染页面时自动切换浏览器服务（需配置 YGG_BROWSER_URL）", type: "bool" },
    { key: "remote_fetch", label: "DeepSeek 远程抓取兜底", hint: "本地与浏览器都无法读取时，尝试由 DeepSeek 搜索源远程抓取", type: "bool" },
  ],
  web_search: [
    { key: "search_results", label: "默认返回结果数", hint: "同时影响 search_x 的新闻模式返回条数", type: "int", unit: "条", min: 1, max: 20 },
  ],
  read_x: [
    { key: "read_url_max_chars", label: "单页最大字符数", hint: "与 read_url 共用分页字数上限", type: "int", unit: "字", min: 1000, max: 100000 },
  ],
  search_x: [
    { key: "search_x_default_hours", label: "默认回溯时间", hint: "模型未指定 hours 参数时往前搜索的小时数", type: "int", unit: "小时", min: 1, max: 720 },
    { key: "search_results", label: "默认返回结果数", hint: "X API 新闻模式最多返回条数（与 web_search 共用）", type: "int", unit: "条", min: 1, max: 20 },
  ],
  read_file: [
    { key: "file_max_mb", label: "附件下载上限", hint: "也作用于工作区 sandbox_put 与 check_provenance 下载限额", type: "int", unit: "MB", min: 1, max: 100 },
    { key: "file_max_pages", label: "PDF 最大解析页数", type: "int", unit: "页", min: 1, max: 200 },
    { key: "file_max_chars", label: "提取正文最大字符数", hint: "超出部分截断并提示模型", type: "int", unit: "字", min: 2000, max: 200000 },
  ],
  search_history: [
    { key: "history_top_k", label: "检索返回条数", hint: "BM25 + 向量混合检索单次返回条数", type: "int", unit: "条", min: 1, max: 50 },
  ],
  get_messages_around: [
    { key: "messages_around_max", label: "单侧最大拉取条数", hint: "before / after 各自允许拉取的最大消息数", type: "int", unit: "条", min: 5, max: 100 },
  ],
  lookup_glossary: [
    { key: "glossary_list_max", label: "词条总览返回上限", hint: "传入空字符串列出全部黑话词条时的上限", type: "int", unit: "条", min: 10, max: 1000 },
  ],
  render_mermaid: [
    { key: "render_max_source_chars", label: "源码最大字符数", hint: "与全部 render_* 工具共用", type: "int", unit: "字", min: 1000, max: 100000 },
    { key: "render_timeout_s", label: "渲染超时时间", hint: "与全部 render_* 工具共用", type: "int", unit: "秒", min: 5, max: 120 },
  ],
  render_markdown: [
    { key: "render_markdown_max_pages", label: "单次最多渲染页数", hint: "长 Markdown 分页输出 PNG 时的上限", type: "int", unit: "页", min: 1, max: 8 },
    { key: "render_max_source_chars", label: "源码最大字符数", hint: "与全部 render_* 工具共用", type: "int", unit: "字", min: 1000, max: 100000 },
    { key: "render_timeout_s", label: "渲染超时时间", hint: "与全部 render_* 工具共用", type: "int", unit: "秒", min: 5, max: 120 },
  ],
  render_html: [
    { key: "render_max_source_chars", label: "源码最大字符数", hint: "与全部 render_* 工具共用", type: "int", unit: "字", min: 1000, max: 100000 },
    { key: "render_timeout_s", label: "渲染超时时间", hint: "与全部 render_* 工具共用", type: "int", unit: "秒", min: 5, max: 120 },
  ],
  generate_image: [
    { key: "generate_image_max_references", label: "参考图最大张数", hint: "改图/参考生图时单次允许传入的图片上限", type: "int", unit: "张", min: 0, max: 10 },
  ],
};

const SANDBOX_GROUPS: { id: string; label: string; fields: ParamFieldSpec[] }[] = [
  {
    id: "exec",
    label: "基础与命令",
    fields: [
      { key: "sandbox_enabled", label: "工作区全局总开关", hint: "关闭后所有频道均不挂载工作区工具", type: "bool" },
      { key: "sandbox_command_timeout_s", label: "单次命令默认超时", hint: "sh / py 未传 timeout 时的默认秒数", type: "int", unit: "秒", min: 1, max: 300 },
      { key: "sandbox_command_max_s", label: "单次命令最长超时", hint: "必须大于等于默认超时", type: "int", unit: "秒", min: 1, max: 1800 },
      { key: "sandbox_invocation_s", label: "单次请求累计执行上限", hint: "一次回复中 sh / py 累计耗时上限（需小于闲置回收时间）", type: "int", unit: "秒", min: 30, max: 3600 },
      { key: "sandbox_max_commands", label: "单次请求最大命令次数", type: "int", unit: "次", min: 1, max: 200 },
    ],
  },
  {
    id: "quota",
    label: "生命周期与配额",
    fields: [
      { key: "sandbox_idle_min", label: "闲置回收时间", hint: "换算为秒必须大于单次请求累计执行上限", type: "int", unit: "分钟", min: 5, max: 1440 },
      { key: "sandbox_max_age_min", label: "最长存活时间", type: "int", unit: "分钟", min: 10, max: 10080 },
      { key: "sandbox_max_workspaces", label: "全局工作区总数上限", type: "int", unit: "个", min: 1, max: 50 },
      { key: "sandbox_max_workspaces_per_channel", label: "单频道工作区上限", type: "int", unit: "个", min: 1, max: 10 },
      { key: "sandbox_max_links", label: "单工作区公开链接上限", type: "int", unit: "个", min: 0, max: 20 },
    ],
  },
  {
    id: "budget",
    label: "预算与工具桥接",
    fields: [
      { key: "sandbox_request_limit", label: "有工作区时模型请求上限", type: "int", unit: "次", min: 1, max: 200 },
      { key: "sandbox_tool_calls_limit", label: "有工作区时工具调用上限", type: "int", unit: "次", min: 1, max: 400 },
      { key: "bridge_run_limit", label: "单次对话工具桥接调用上限", hint: "工作区脚本通过 bridge 调用 bot 工具的上限", type: "int", unit: "次", min: 0, max: 500 },
      { key: "bridge_job_limit", label: "单个后台任务工具桥接上限", type: "int", unit: "次", min: 0, max: 2000 },
    ],
  },
  {
    id: "jobs",
    label: "后台任务",
    fields: [
      { key: "sandbox_max_jobs", label: "每频道并发后台任务上限", hint: "设为 0 关闭后台任务", type: "int", unit: "个", min: 0, max: 10 },
      { key: "sandbox_job_command_min", label: "后台命令最长运行时间", type: "int", unit: "分钟", min: 1, max: 600 },
      { key: "sandbox_delegate", label: "启用后台大任务（delegate）", hint: "需同时配置 YGG_RELAY_URL 与 agent 模型路由", type: "bool" },
      { key: "sandbox_job_agent_min", label: "后台大任务最长运行时间", type: "int", unit: "分钟", min: 1, max: 240 },
      { key: "sandbox_job_max_input_tokens", label: "后台大任务输入 Token 上限", type: "int", min: 10000 },
      { key: "sandbox_job_max_output_tokens", label: "后台大任务输出 Token 上限", type: "int", min: 1000 },
    ],
  },
];

const BUILTIN_SKILL_PARAM_SPECS: Record<string, ParamFieldSpec[]> = {
  kook: [
    { key: "kook_upload_max_mb", label: "upload_file 上传大小上限", hint: "从工作区上传文件到 KOOK 频道的单文件上限", type: "int", unit: "MB", min: 1, max: 50 },
    { key: "kook_pinned_shown", label: "channel_info 置顶消息展示条数", type: "int", unit: "条", min: 1, max: 20 },
    { key: "kook_thread_body_chars", label: "帖子主楼正文截断长度", hint: "read_forum_thread 返回主楼正文的最大字符数", type: "int", unit: "字", min: 1000, max: 30000 },
  ],
  stickers: [
    { key: "stickers_pool_limit", label: "表情包检索候选池上限", hint: "search_stickers 按热度预加载的最大表情包数", type: "int", unit: "张", min: 20, max: 1000 },
  ],
};

function statusBadgeVariant(status: ToolStatus): "default" | "secondary" | "destructive" | "outline" {
  if (status === "ready") return "secondary";
  if (status === "warning") return "outline";
  if (status === "unconfigured") return "destructive";
  return "outline";
}

function summarizeParams(tool: ToolItemOut): string[] {
  const p = tool.params ?? {};
  const out: string[] = [];
  if (tool.id === "check_provenance") {
    out.push(`超时 ${p.provenance_timeout_s}s`, `上限 ${p.provenance_max_mb}MB`);
    if (tool.secret?.enabled && tool.secret.api_key_masked) out.push(`独立 Key ${tool.secret.api_key_masked}`);
  } else if (tool.id === "read_url") {
    out.push(`单页 ${p.read_url_max_chars} 字`, p.browser_fallback ? "浏览器回退开" : "浏览器回退关", p.remote_fetch ? "远程抓取开" : "远程抓取关");
  } else if (tool.id === "read_x") {
    out.push(`单页 ${p.read_url_max_chars} 字`);
  } else if (tool.id === "web_search") {
    out.push(`返回 ${p.search_results} 条`);
  } else if (tool.id === "search_x") {
    out.push(`默认回溯 ${p.search_x_default_hours}h`, `新闻 ${p.search_results} 条`);
  } else if (tool.id === "read_file") {
    out.push(`${p.file_max_mb}MB`, `最多 ${p.file_max_pages} 页`, `${p.file_max_chars} 字`);
  } else if (tool.id === "search_history") {
    out.push(`Top ${p.history_top_k}`);
  } else if (tool.id === "get_messages_around") {
    out.push(`单侧最多 ${p.messages_around_max} 条`);
  } else if (tool.id === "lookup_glossary") {
    out.push(`总览上限 ${p.glossary_list_max} 条`);
  } else if (tool.id === "render_markdown") {
    out.push(`最多 ${p.render_markdown_max_pages} 页`, `超时 ${p.render_timeout_s}s`, `源码 ${p.render_max_source_chars} 字`);
  } else if (tool.id === "render_mermaid" || tool.id === "render_html") {
    out.push(`超时 ${p.render_timeout_s}s`, `源码 ${p.render_max_source_chars} 字`);
  } else if (tool.id === "generate_image") {
    out.push(`参考图最多 ${p.generate_image_max_references} 张`);
  } else if (tool.id === "sandbox") {
    out.push(
      p.sandbox_enabled ? "总开关开" : "总开关关",
      `命令超时 ${p.sandbox_command_timeout_s}s / ${p.sandbox_command_max_s}s`,
      `工作区上限 ${p.sandbox_max_workspaces}`,
      `后台任务 ${p.sandbox_max_jobs}`,
    );
  }
  if (tool.slow) out.push("计入耗时");
  return out;
}

export function ToolsPage() {
  const toolsQ = useQuery(getToolsOverviewOptions());
  const skillsQ = useQuery(getSkillsOverviewOptions());
  const providersQ = useQuery(listProvidersOptions());

  return (
    <div className="max-w-6xl">
      <PageHeader
        title="工具与技能"
        description={
          <>
            配置每个工具自身的参数、连接凭据与在线测试，并管理内置及自定义 Skills。按服务器 / 分组 / 频道 / 角色控制使用权限请在
            <Link to="/channels" className="mx-1 text-primary underline-offset-4 hover:underline">
              服务器与频道
            </Link>
            中设置。
          </>
        }
      />

      <Tabs defaultValue="tools">
        <TabsList className="mb-4">
          <TabsTrigger value="tools" className="gap-1.5">
            <WrenchIcon className="size-3.5" aria-hidden />
            工具配置
            {toolsQ.data && <Badge variant="secondary" className="ml-1 text-[11px]">{toolsQ.data.items.length}</Badge>}
          </TabsTrigger>
          <TabsTrigger value="skills" className="gap-1.5">
            <SparklesIcon className="size-3.5" aria-hidden />
            Skills 技能
            {skillsQ.data && (
              <Badge variant="secondary" className="ml-1 text-[11px]">
                {skillsQ.data.builtin.length + skillsQ.data.custom.length}
              </Badge>
            )}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="tools">
          <ToolsTab
            data={toolsQ.data}
            isLoading={toolsQ.isLoading}
            error={toolsQ.error}
            providers={providersQ.data ?? []}
          />
        </TabsContent>

        <TabsContent value="skills">
          <SkillsTab data={skillsQ.data} isLoading={skillsQ.isLoading} error={skillsQ.error} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// --- Tab 1: Tools Configuration ------------------------------------------------------

function ToolsTab({
  data,
  isLoading,
  error,
  providers,
}: {
  data: { items: ToolItemOut[]; general: { request_limit: number; tool_calls_limit: number; slow_tools: string[] }; runtime_flags: Record<string, boolean> } | undefined;
  isLoading: boolean;
  error: unknown;
  providers: ProviderOut[];
}) {
  const [category, setCategory] = useState<ToolCategory | "all">("all");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<ToolItemOut | null>(null);
  const [testingProvenance, setTestingProvenance] = useState(false);

  const [reqLimit, setReqLimit] = useState<number>(12);
  const [callLimit, setCallLimit] = useState<number>(16);

  useEffect(() => {
    if (data) {
      setReqLimit(data.general.request_limit);
      setCallLimit(data.general.tool_calls_limit);
    }
  }, [data?.general.request_limit, data?.general.tool_calls_limit]);

  const updateTool = useMutation({
    ...updateToolConfigMutation(),
    onSuccess: () => toast.success("已保存工具配置并热更新"),
  });

  const updateGeneral = useMutation({
    ...updateGeneralToolPolicyMutation(),
    onSuccess: () => toast.success("已更新通用执行预算"),
  });

  if (!data) {
    return <QueryState isLoading={isLoading} error={error} variant="card" />;
  }

  const generalDirty =
    reqLimit !== data.general.request_limit || callLimit !== data.general.tool_calls_limit;

  const q = query.trim().toLowerCase();
  const filtered = data.items.filter((item) => {
    if (category !== "all" && item.category !== category) return false;
    if (!q) return true;
    return (
      item.id.toLowerCase().includes(q) ||
      item.name.toLowerCase().includes(q) ||
      item.description.toLowerCase().includes(q) ||
      item.status_detail.toLowerCase().includes(q)
    );
  });

  const grouped = CATEGORY_META.filter((c) => c.value !== "all")
    .map((c) => ({
      category: c.value as ToolCategory,
      label: c.label,
      items: filtered.filter((i) => i.category === c.value),
    }))
    .filter((g) => g.items.length > 0);

  return (
    <div className="grid gap-6">
      <Card>
        <CardHeader className="py-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <CardTitle className="text-base">通用调用预算</CardTitle>
              <CardDescription className="text-xs">
                普通对话单次运行的模型轮次与工具调用上限（开启沙盒工作区时的独立上限请在「工作区套件」内配置）。
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-end gap-3">
              <div className="grid gap-1">
                <span className="text-xs text-muted-foreground">单次请求上限</span>
                <NumberInput
                  aria-label="单次请求上限"
                  className="h-8 w-24"
                  value={reqLimit}
                  onChange={(v) => v !== null && setReqLimit(v)}
                />
              </div>
              <div className="grid gap-1">
                <span className="text-xs text-muted-foreground">工具调用上限</span>
                <NumberInput
                  aria-label="工具调用上限"
                  className="h-8 w-24"
                  value={callLimit}
                  onChange={(v) => v !== null && setCallLimit(v)}
                />
              </div>
              <PendingButton
                size="sm"
                pending={updateGeneral.isPending}
                disabled={!generalDirty}
                onClick={() =>
                  updateGeneral.mutate({
                    body: { request_limit: reqLimit, tool_calls_limit: callLimit },
                  })
                }
              >
                {generalDirty ? "保存预算" : "已同步"}
              </PendingButton>
            </div>
          </div>
        </CardHeader>
      </Card>

      <FilterBar>
        <div className="relative w-64">
          <SearchIcon className="pointer-events-none absolute top-2.5 left-2.5 size-4 text-muted-foreground" aria-hidden />
          <Input
            aria-label="筛选工具"
            placeholder="搜索工具名、ID 或说明…"
            className="pl-8"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="flex flex-wrap gap-1.5">
          {CATEGORY_META.map((c) => (
            <Button
              key={c.value}
              size="sm"
              variant={category === c.value ? "default" : "outline"}
              onClick={() => setCategory(c.value)}
            >
              {c.label}
            </Button>
          ))}
        </div>
      </FilterBar>

      {grouped.length === 0 && <Empty description="尝试更换分类或清空搜索关键词。">没有匹配的工具</Empty>}

      {grouped.map((group) => (
        <section key={group.category} className="grid gap-3">
          <SectionHeader title={group.label} />
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {group.items.map((tool) => {
              const summary = summarizeParams(tool);
              return (
                <Card key={tool.id} className={cn("flex flex-col", !tool.enabled && "opacity-75")}>
                  <CardHeader className="pb-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <CardTitle className="text-base">{tool.name}</CardTitle>
                          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                            {tool.id}
                          </code>
                          {tool.supports_secret && (
                            <Badge variant="outline" className="gap-1 text-[11px] font-normal">
                              <KeyRoundIcon className="size-3" aria-hidden /> 凭据可配
                            </Badge>
                          )}
                        </div>
                        <CardDescription className="mt-1 text-xs leading-relaxed">
                          {tool.description}
                        </CardDescription>
                      </div>
                      <Switch
                        aria-label={`全局默认启用 ${tool.name}`}
                        checked={tool.enabled}
                        disabled={updateTool.isPending}
                        onCheckedChange={(enabled) =>
                          updateTool.mutate({ path: { tool_id: tool.id }, body: { enabled } })
                        }
                      />
                    </div>
                  </CardHeader>
                  <CardContent className="grid flex-1 gap-2 pt-0">
                    <div className="flex flex-wrap items-center gap-1.5 text-xs">
                      <Badge variant={statusBadgeVariant(tool.status)}>{tool.status_label}</Badge>
                      <span className="text-muted-foreground">{tool.status_detail}</span>
                    </div>
                    {summary.length > 0 && (
                      <div className="flex flex-wrap gap-1.5">
                        {summary.map((chip) => (
                          <Badge key={chip} variant="outline" className="text-[11px] font-normal text-muted-foreground">
                            {chip}
                          </Badge>
                        ))}
                      </div>
                    )}
                  </CardContent>
                  <CardFooter className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
                    <Meta
                      className="text-[11px]"
                      items={[
                        `场景：${tool.handlers.join(" / ")}`,
                        CATEGORY_LABELS[tool.category] ?? tool.category,
                      ]}
                    />
                    <div className="flex items-center gap-1.5">
                      {tool.related_route && (
                        <Button size="sm" variant="ghost" asChild>
                          <Link to={tool.related_route}>
                            {tool.related_label ?? "相关页面"}
                            <ArrowUpRightIcon data-icon="inline-end" className="size-3.5" />
                          </Link>
                        </Button>
                      )}
                      {tool.id === "check_provenance" && (
                        <Button size="sm" variant="outline" onClick={() => setTestingProvenance(true)}>
                          <ZapIcon data-icon="inline-start" /> 测试
                        </Button>
                      )}
                      <Button size="sm" variant="outline" onClick={() => setEditing(tool)}>
                        <Settings2Icon data-icon="inline-start" /> 配置
                      </Button>
                    </div>
                  </CardFooter>
                </Card>
              );
            })}
          </div>
        </section>
      ))}

      {editing && (
        <ToolConfigDialog
          tool={data.items.find((i) => i.id === editing.id) ?? editing}
          providers={providers}
          runtimeFlags={data.runtime_flags}
          onClose={() => setEditing(null)}
        />
      )}

      {testingProvenance && (
        <ProvenanceTestDialog onClose={() => setTestingProvenance(false)} />
      )}
    </div>
  );
}

type ProvenanceMode = "auto" | "provider" | "custom";

function ToolConfigDialog({
  tool,
  providers,
  runtimeFlags,
  onClose,
}: {
  tool: ToolItemOut;
  providers: ProviderOut[];
  runtimeFlags: Record<string, boolean>;
  onClose: () => void;
}) {
  const [enabled, setEnabled] = useState(tool.enabled);
  const [slow, setSlow] = useState(tool.slow);
  const [params, setParams] = useState<Record<string, unknown>>(() => ({ ...(tool.params ?? {}) }));

  // check_provenance dedicated credential state
  const initialMode: ProvenanceMode =
    tool.id === "check_provenance"
      ? String(tool.params?.provenance_provider ?? "").trim()
        ? "provider"
        : tool.secret?.enabled
          ? "custom"
          : "auto"
      : "auto";
  const [provMode, setProvMode] = useState<ProvenanceMode>(initialMode);
  const [provRef, setProvRef] = useState<string>(String(tool.params?.provenance_provider ?? ""));
  const [secBaseUrl, setSecBaseUrl] = useState<string>(tool.secret?.base_url ?? "");
  const [secApiKey, setSecApiKey] = useState<string>("");
  const [secClearKey, setSecClearKey] = useState<boolean>(false);
  const [secHeaders, setSecHeaders] = useState<Record<string, string>>(() => ({ ...(tool.secret?.headers ?? {}) }));
  const [headersValid, setHeadersValid] = useState(true);

  const save = useMutation({
    ...updateToolConfigMutation(),
    onSuccess: () => {
      toast.success(`已保存「${tool.name}」配置并热更新`);
      onClose();
    },
  });

  const setParam = (key: string, val: unknown) => setParams((cur) => ({ ...cur, [key]: val }));
  const resetDefaults = () => {
    setParams({ ...(tool.defaults ?? {}) });
    if (tool.id === "check_provenance") {
      setProvMode("auto");
      setProvRef("");
    }
  };

  const submit = () => {
    const bodyParams: Record<string, unknown> = { ...params };
    let secretPayload: ToolSecretIn | null = null;
    if (tool.id === "check_provenance") {
      bodyParams.provenance_provider = provMode === "provider" ? provRef.trim() : "";
      secretPayload = {
        enabled: provMode === "custom",
        base_url: secBaseUrl.trim(),
        api_key: secApiKey.trim() || null,
        clear_api_key: secClearKey,
        headers: secHeaders,
      };
    }
    save.mutate({
      path: { tool_id: tool.id },
      body: {
        enabled,
        slow,
        params: bodyParams,
        secret: secretPayload,
      },
    });
  };

  const fields = TOOL_PARAM_SPECS[tool.id] ?? [];
  const isSandbox = tool.id === "sandbox";
  const isProvenance = tool.id === "check_provenance";
  const hasParams = fields.length > 0 || isSandbox || isProvenance;

  const providerOptions = providers.map((p) => ({
    value: p.name,
    label: `${p.name}（${p.api_format}${p.enabled ? "" : " · 已停用"} · ${p.base_url}）`,
  }));

  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`配置 ${tool.name}（${tool.id}）`}
      description={tool.description}
      onSubmit={submit}
      pending={save.isPending}
      submitDisabled={(isProvenance && provMode === "custom" && !headersValid) || (isProvenance && provMode === "provider" && !provRef.trim())}
      className={isSandbox || isProvenance ? "sm:max-w-2xl" : "sm:max-w-lg"}
    >
      <div className="grid gap-3 rounded-lg border bg-muted/20 p-3">
        <Field
          label="全局默认启用"
          hint="控制该工具的全局默认开关；服务器 / 分组 / 频道与角色权限覆盖请在「服务器与频道」中单独配置。"
          orientation="horizontal"
        >
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </Field>
        <Field
          label="在元数据页脚显示耗时（slow_tools）"
          hint="开启后，调用本工具的累计耗时将展示在回复底部的灰色元数据栏中。"
          orientation="horizontal"
        >
          <Switch checked={slow} onCheckedChange={setSlow} />
        </Field>
      </div>

      {isProvenance && (
        <div className="grid gap-4 rounded-lg border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">OpenAI Content Provenance 连接设置</span>
            <Badge variant={statusBadgeVariant(tool.status)}>{tool.status_label}</Badge>
          </div>
          <Field
            label="凭据来源模式"
            hint={
              runtimeFlags.openai_env_key_configured
                ? "自动模式优先匹配指向 api.openai.com 的已启用 Provider，否则回退至环境变量 OPENAI_API_KEY（当前已配置）。"
                : "自动模式优先匹配指向 api.openai.com 的已启用 Provider（当前未设置环境变量 OPENAI_API_KEY）。"
            }
          >
            <OptionSelect
              value={provMode}
              onChange={(v) => setProvMode((v as ProvenanceMode) ?? "auto")}
              options={[
                { value: "auto", label: "自动匹配（官方 OpenAI Provider 或 OPENAI_API_KEY）" },
                { value: "provider", label: "复用已有 Provider（从控制台 Providers 选择）" },
                { value: "custom", label: "独立加密凭据（单独配置 Base URL 与 API Key）" },
              ]}
            />
          </Field>

          {provMode === "provider" && (
            <Field
              label="选择提供商"
              hint="会向该提供商的 /v1/content_provenance_checks 发送原始文件字节（支持 OpenAI 官方或兼容代理）。"
            >
              {providerOptions.length > 0 ? (
                <OptionSelect
                  value={provRef || null}
                  onChange={(v) => setProvRef(v ?? "")}
                  placeholder="选择一个已配置的 Provider"
                  options={providerOptions}
                />
              ) : (
                <Input
                  placeholder="当前没有已配置的 Provider，可输入名称或先去 Providers 添加"
                  value={provRef}
                  onChange={(e) => setProvRef(e.target.value)}
                />
              )}
            </Field>
          )}

          {provMode === "custom" && (
            <div className="grid gap-3 rounded-md border bg-muted/10 p-3">
              <Field label="Base URL" hint="留空默认使用 https://api.openai.com/v1；如使用代理请填完整 https:// 地址。">
                <Input
                  placeholder="https://api.openai.com/v1"
                  value={secBaseUrl}
                  onChange={(e) => setSecBaseUrl(e.target.value)}
                />
              </Field>
              <ApiKeyField
                masked={tool.secret?.api_key_masked}
                value={secApiKey}
                clear={secClearKey}
                emptyHint="必填（加密存储，只展示末四位）"
                onChange={(patch) => {
                  if (patch.api_key !== undefined) setSecApiKey(patch.api_key);
                  if (patch.clear_api_key !== undefined) setSecClearKey(patch.clear_api_key);
                }}
              />
              <Field label="附加请求头（JSON，可选）" hint='例如 {"OpenAI-Organization": "org-..."}'>
                <JsonEditor
                  rows={3}
                  value={secHeaders}
                  onValidityChange={setHeadersValid}
                  onChange={(v) => isPlainObject(v) && setSecHeaders(v as Record<string, string>)}
                />
              </Field>
            </div>
          )}

          <div className="grid grid-cols-2 gap-4">
            <Field label="请求超时（秒）" hint="范围 5–120 秒，默认 30">
              <NumberInput
                value={typeof params.provenance_timeout_s === "number" ? params.provenance_timeout_s : 30}
                onChange={(v) => v !== null && setParam("provenance_timeout_s", v)}
              />
            </Field>
            <Field label="单文件大小上限（MB）" hint="OpenAI 上限为 25MB，默认 25">
              <NumberInput
                value={typeof params.provenance_max_mb === "number" ? params.provenance_max_mb : 25}
                onChange={(v) => v !== null && setParam("provenance_max_mb", v)}
              />
            </Field>
          </div>
        </div>
      )}

      {isSandbox && (
        <Tabs defaultValue="exec">
          <TabsList className="grid w-full grid-cols-4">
            {SANDBOX_GROUPS.map((g) => (
              <TabsTrigger key={g.id} value={g.id} className="text-xs">
                {g.label}
              </TabsTrigger>
            ))}
          </TabsList>
          {SANDBOX_GROUPS.map((g) => (
            <TabsContent key={g.id} value={g.id} className="grid gap-3 pt-2">
              {g.fields.map((f) => (
                <ParamField
                  key={f.key}
                  spec={f}
                  value={params[f.key]}
                  defaultValue={tool.defaults?.[f.key]}
                  onChange={(v) => setParam(f.key, v)}
                />
              ))}
            </TabsContent>
          ))}
        </Tabs>
      )}

      {fields.length > 0 && (
        <div className="grid gap-3">
          {fields.map((f) => (
            <ParamField
              key={f.key}
              spec={f}
              value={params[f.key]}
              defaultValue={tool.defaults?.[f.key]}
              onChange={(v) => setParam(f.key, v)}
            />
          ))}
        </div>
      )}

      {!hasParams && (
        <p className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground">
          该工具目前没有额外数值参数；你可以调整上方的全局默认启用开关与耗时统计标记。
          {tool.related_route && (
            <>
              {" "}
              相关依赖请在
              <Link to={tool.related_route} className="mx-1 text-primary underline-offset-4 hover:underline">
                {tool.related_label ?? "对应页面"}
              </Link>
              中配置。
            </>
          )}
        </p>
      )}

      {hasParams && (
        <div className="flex justify-end">
          <Button type="button" size="sm" variant="ghost" onClick={resetDefaults}>
            <RotateCcwIcon data-icon="inline-start" /> 恢复默认参数
          </Button>
        </div>
      )}
    </FormDialog>
  );
}

function ParamField({
  spec,
  value,
  defaultValue,
  onChange,
}: {
  spec: ParamFieldSpec;
  value: unknown;
  defaultValue: unknown;
  onChange: (v: unknown) => void;
}) {
  const hintParts: string[] = [];
  if (spec.hint) hintParts.push(spec.hint);
  if (defaultValue !== undefined) {
    const defText = typeof defaultValue === "boolean" ? (defaultValue ? "开启" : "关闭") : `${defaultValue}${spec.unit ?? ""}`;
    hintParts.push(`默认：${defText}`);
  }
  const hint = hintParts.join(" · ");

  if (spec.type === "bool") {
    return (
      <Field label={spec.label} hint={hint} orientation="horizontal">
        <Switch checked={Boolean(value)} onCheckedChange={onChange} />
      </Field>
    );
  }
  return (
    <Field label={spec.unit ? `${spec.label}（${spec.unit}）` : spec.label} hint={hint}>
      <NumberInput
        className="max-w-48"
        value={typeof value === "number" ? value : null}
        onChange={(v) => v !== null && onChange(v)}
      />
    </Field>
  );
}

function ProvenanceTestDialog({ onClose }: { onClose: () => void }) {
  const [selectedFile, setSelectedFile] = useState<{ name: string; mime: string; size: number; b64: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const test = useMutation(testProvenanceToolMutation());
  const result = test.data;

  const pickFile = (file: File | undefined) => {
    if (!file) {
      setSelectedFile(null);
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      toast.error("文件超过 OpenAI 25MB 限制");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = typeof reader.result === "string" ? reader.result : "";
      const b64 = dataUrl.includes(",") ? dataUrl.split(",", 2)[1] : dataUrl;
      setSelectedFile({
        name: file.name,
        mime: file.type || "",
        size: file.size,
        b64,
      });
    };
    reader.readAsDataURL(file);
  };

  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="测试 OpenAI 内容溯源检测"
      description="按当前已保存的配置向 /v1/content_provenance_checks 发起真实请求。不选文件时会自动发送 1×1 透明 PNG 做连通性与鉴权探活。"
      onSubmit={() =>
        test.mutate({
          body: selectedFile
            ? { file_b64: selectedFile.b64, filename: selectedFile.name, mime: selectedFile.mime }
            : {},
        })
      }
      pending={test.isPending}
      submitLabel={selectedFile ? "检测所选文件" : "快速探活 (1×1 PNG)"}
      className="sm:max-w-xl"
    >
      <Field label="测试文件（可选）" hint="支持图片（PNG/JPEG/WebP 等）、音频或 PDF，原始字节直传不做重压缩。">
        <div className="flex flex-wrap items-center gap-2">
          <input
            ref={inputRef}
            type="file"
            accept="image/*,audio/*,.pdf"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0])}
          />
          <Button type="button" size="sm" variant="outline" onClick={() => inputRef.current?.click()}>
            <UploadIcon data-icon="inline-start" /> 选择本地文件
          </Button>
          {selectedFile ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="max-w-64 truncate font-mono text-foreground">{selectedFile.name}</span>
              <span>({(selectedFile.size / 1024).toFixed(1)} KB)</span>
              <Button type="button" size="xs" variant="ghost" onClick={() => setSelectedFile(null)}>
                清除
              </Button>
            </div>
          ) : (
            <span className="text-xs text-muted-foreground">未选择文件（将使用内置 1×1 PNG 探活）</span>
          )}
        </div>
      </Field>

      {result && !result.ok && (
        <Alert variant="destructive">
          <AlertTitle>检测失败</AlertTitle>
          <AlertDescription className="grid gap-1">
            <span className="whitespace-pre-wrap">{result.error}</span>
            <Meta items={[`来源：${result.source}`, result.endpoint || null, `耗时 ${result.latency_ms}ms`]} />
          </AlertDescription>
        </Alert>
      )}

      {result?.ok && (
        <Alert>
          <CheckCircle2Icon className="size-4" />
          <AlertTitle>接口连通正常</AlertTitle>
          <AlertDescription className="grid gap-2">
            <Meta
              items={[
                `来源：${result.source}`,
                `文件：${result.filename}`,
                `耗时 ${result.latency_ms}ms`,
                result.any_detected ? "检测到溯源信号" : "未检测到溯源信号",
              ]}
            />
            {result.report && (
              <pre className="rounded-md bg-muted p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
                {result.report}
              </pre>
            )}
          </AlertDescription>
        </Alert>
      )}
    </FormDialog>
  );
}

// --- Tab 2: Skills Management --------------------------------------------------------

function SkillsTab({
  data,
  isLoading,
  error,
}: {
  data: { builtin: BuiltinSkillOut[]; custom: CustomSkillOut[] } | undefined;
  isLoading: boolean;
  error: unknown;
}) {
  const [editingBuiltin, setEditingBuiltin] = useState<BuiltinSkillOut | null>(null);
  const [editingCustom, setEditingCustom] = useState<CustomSkillOut | "new" | "import" | null>(null);

  const toggleBuiltin = useMutation({
    ...updateBuiltinSkillMutation(),
    onSuccess: () => toast.success("已更新内置技能设置"),
  });

  const updateCustom = useMutation({
    ...updateCustomSkillMutation(),
    onSuccess: () => toast.success("已更新自定义技能状态"),
  });

  const removeCustom = useMutation({
    ...deleteCustomSkillMutation(),
    onSuccess: () => toast.success("已删除自定义技能"),
  });

  if (!data) {
    return <QueryState isLoading={isLoading} error={error} variant="card" />;
  }

  return (
    <div className="grid gap-8">
      <section className="grid gap-3">
        <SectionHeader
          title="自定义 Skills"
          description="新增领域知识、社区规范、SOP 工作流等纯指令型技能。按需加载模式下模型仅看到简短描述，相关时自动加载完整正文。"
          actions={
            <>
              <Button size="sm" variant="outline" onClick={() => setEditingCustom("import")}>
                <FileUpIcon data-icon="inline-start" /> 导入 SKILL.md
              </Button>
              <Button size="sm" onClick={() => setEditingCustom("new")}>
                <PlusIcon data-icon="inline-start" /> 新建 Skill
              </Button>
            </>
          }
        />

        {data.custom.length === 0 ? (
          <Empty
            icon={<SparklesIcon />}
            description="你可以新建自定义 Skill，或直接粘贴现成的 SKILL.md（含 YAML frontmatter）快速导入。"
            action={
              <div className="flex gap-2">
                <Button size="sm" onClick={() => setEditingCustom("new")}>
                  <PlusIcon data-icon="inline-start" /> 新建 Skill
                </Button>
                <Button size="sm" variant="outline" onClick={() => setEditingCustom("import")}>
                  <FileUpIcon data-icon="inline-start" /> 导入 SKILL.md
                </Button>
              </div>
            }
          >
            还没有自定义 Skill
          </Empty>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {data.custom.map((skill) => (
              <Card key={skill.id} className={cn("flex flex-col", !skill.enabled && "opacity-70")}>
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <CardTitle className="text-base">{skill.name}</CardTitle>
                        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                          {skill.id}
                        </code>
                      </div>
                      <CardDescription className="mt-1 line-clamp-2 text-xs leading-relaxed">
                        {skill.description}
                      </CardDescription>
                    </div>
                    <Switch
                      aria-label={`启用技能 ${skill.name}`}
                      checked={skill.enabled}
                      disabled={updateCustom.isPending}
                      onCheckedChange={(enabled) =>
                        updateCustom.mutate({
                          path: { skill_id: skill.id },
                          body: {
                            name: skill.name,
                            description: skill.description,
                            instructions: skill.instructions,
                            defer_loading: skill.defer_loading,
                            handlers: skill.handlers,
                            allow_private: skill.allow_private,
                            default_active: skill.default_active,
                            enabled,
                          },
                        })
                      }
                    />
                  </div>
                </CardHeader>
                <CardContent className="flex-1 pt-0">
                  <div className="flex flex-wrap gap-1.5">
                    <Badge variant={skill.defer_loading ? "secondary" : "default"} className="text-[11px]">
                      {skill.defer_loading ? "按需加载" : "常驻注入"}
                    </Badge>
                    <Badge variant="outline" className="text-[11px] font-normal">
                      {skill.default_active ? "全局默认生效" : "仅指定频道生效"}
                    </Badge>
                    <Badge variant="outline" className="text-[11px] font-normal">
                      场景：{(skill.handlers ?? ["chat"]).join(" / ")}
                    </Badge>
                    <Badge variant="outline" className="text-[11px] font-normal">
                      {skill.allow_private ? "含私聊" : "仅频道"}
                    </Badge>
                    <Badge variant="outline" className="text-[11px] font-normal text-muted-foreground">
                      正文 {skill.instructions.length} 字
                    </Badge>
                  </div>
                </CardContent>
                <CardFooter className="flex justify-end gap-1.5 border-t pt-3">
                  <Button size="sm" variant="outline" onClick={() => setEditingCustom(skill)}>
                    <PencilIcon data-icon="inline-start" /> 编辑
                  </Button>
                  <ConfirmButton
                    title={`删除自定义 Skill「${skill.name}」？`}
                    description="删除后将立即停止加载该技能，无法恢复。"
                    onConfirm={() => removeCustom.mutate({ path: { skill_id: skill.id } })}
                  >
                    删除
                  </ConfirmButton>
                </CardFooter>
              </Card>
            ))}
          </div>
        )}
      </section>

      <section className="grid gap-3">
        <SectionHeader
          title="内置 Skills"
          description="代码内置的按需工具技能包；可配置它们自身的运行参数，并可追加自定义补充指令。"
        />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {data.builtin.map((skill) => (
            <Card key={skill.id} className={cn("flex flex-col", !skill.enabled && "opacity-75")}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <CardTitle className="text-base">{skill.name}</CardTitle>
                      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                        {skill.id}
                      </code>
                      <Badge variant="secondary" className="text-[11px]">内置</Badge>
                    </div>
                    <CardDescription className="mt-1 text-xs leading-relaxed">
                      {skill.description}
                    </CardDescription>
                  </div>
                  <Switch
                    aria-label={`全局默认加载 ${skill.name}`}
                    checked={skill.enabled}
                    disabled={toggleBuiltin.isPending}
                    onCheckedChange={(enabled) =>
                      toggleBuiltin.mutate({ path: { skill_id: skill.id }, body: { enabled } })
                    }
                  />
                </div>
              </CardHeader>
              <CardContent className="grid flex-1 gap-2 pt-0">
                <div className="flex flex-wrap gap-1">
                  {skill.tools.map((t) => (
                    <Badge key={t} variant="outline" className="font-mono text-[11px] font-normal">
                      {t}
                    </Badge>
                  ))}
                </div>
                <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
                  {Object.entries(skill.params ?? {}).map(([k, v]) => (
                    <Badge key={k} variant="outline" className="text-[11px] font-normal text-muted-foreground">
                      {k}={String(v)}
                    </Badge>
                  ))}
                  {skill.extra_instructions && (
                    <Badge variant="secondary" className="text-[11px] font-normal">
                      已设补充指令（{skill.extra_instructions.length} 字）
                    </Badge>
                  )}
                </div>
              </CardContent>
              <CardFooter className="flex justify-end border-t pt-3">
                <Button size="sm" variant="outline" onClick={() => setEditingBuiltin(skill)}>
                  <Settings2Icon data-icon="inline-start" /> 配置
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
      </section>

      {editingBuiltin && (
        <BuiltinSkillDialog
          skill={data.builtin.find((s) => s.id === editingBuiltin.id) ?? editingBuiltin}
          onClose={() => setEditingBuiltin(null)}
        />
      )}

      {editingCustom && (
        <CustomSkillDialog
          skill={editingCustom === "new" || editingCustom === "import" ? null : editingCustom}
          defaultImportOpen={editingCustom === "import"}
          onClose={() => setEditingCustom(null)}
        />
      )}
    </div>
  );
}

function BuiltinSkillDialog({ skill, onClose }: { skill: BuiltinSkillOut; onClose: () => void }) {
  const [enabled, setEnabled] = useState(skill.enabled);
  const [params, setParams] = useState<Record<string, unknown>>(() => ({ ...(skill.params ?? {}) }));
  const [extra, setExtra] = useState(skill.extra_instructions ?? "");

  const save = useMutation({
    ...updateBuiltinSkillMutation(),
    onSuccess: () => {
      toast.success(`已保存内置技能「${skill.name}」配置`);
      onClose();
    },
  });

  const specs = BUILTIN_SKILL_PARAM_SPECS[skill.id] ?? [];

  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={`配置内置技能：${skill.name}（${skill.id}）`}
      description="调整内置技能自身的参数，或追加额外说明（会拼接到内置指令末尾，留空即使用原版内置说明）。"
      onSubmit={() =>
        save.mutate({
          path: { skill_id: skill.id },
          body: { enabled, params, extra_instructions: extra },
        })
      }
      pending={save.isPending}
      className="sm:max-w-xl"
    >
      <Field label="全局默认启用" orientation="horizontal">
        <Switch checked={enabled} onCheckedChange={setEnabled} />
      </Field>
      {specs.map((f) => (
        <ParamField
          key={f.key}
          spec={f}
          value={params[f.key]}
          defaultValue={skill.defaults?.[f.key]}
          onChange={(v) => setParams((cur) => ({ ...cur, [f.key]: v }))}
        />
      ))}
      <Field
        label="补充指令（可选）"
        hint="会在模型加载该技能时附加到内置 INSTRUCTIONS 末尾，例如补充本社区的发帖/表情包习惯。"
      >
        <Textarea
          rows={6}
          placeholder="留空表示仅使用内置说明…"
          value={extra}
          onChange={(e) => setExtra(e.target.value)}
        />
      </Field>
      <div className="flex justify-end">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => {
            setParams({ ...(skill.defaults ?? {}) });
            setExtra("");
          }}
        >
          <RotateCcwIcon data-icon="inline-start" /> 恢复默认
        </Button>
      </div>
    </FormDialog>
  );
}

/** Parse standard SKILL.md YAML frontmatter (`name`, `description`) + markdown body. */
function parseSkillMarkdown(raw: string): { id?: string; name?: string; description?: string; instructions: string } {
  const text = raw.replace(/\r\n/g, "\n").trim();
  const match = text.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!match) return { instructions: text };
  const fm = match[1];
  const body = match[2].trim();
  const grab = (key: string): string | undefined => {
    const m = fm.match(new RegExp(`^${key}\\s*:\\s*(.+)$`, "m"));
    if (!m) return undefined;
    return m[1].trim().replace(/^["']|["']$/g, "");
  };
  const name = grab("name");
  const description = grab("description");
  const rawId = grab("id") || name;
  const slug = rawId
    ? rawId
        .toLowerCase()
        .replace(/[^a-z0-9_-]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 48)
    : undefined;
  return {
    id: slug && /^[a-z0-9]/.test(slug) ? slug : undefined,
    name,
    description,
    instructions: body,
  };
}

function CustomSkillDialog({
  skill,
  defaultImportOpen = false,
  onClose,
}: {
  skill: CustomSkillOut | null;
  defaultImportOpen?: boolean;
  onClose: () => void;
}) {
  const isEdit = skill !== null;
  const [id, setId] = useState(skill?.id ?? "");
  const [name, setName] = useState(skill?.name ?? "");
  const [description, setDescription] = useState(skill?.description ?? "");
  const [instructions, setInstructions] = useState(skill?.instructions ?? "");
  const [deferLoading, setDeferLoading] = useState(skill?.defer_loading ?? true);
  const [handlers, setHandlers] = useState<("chat" | "tldr")[]>(skill?.handlers ?? ["chat"]);
  const [allowPrivate, setAllowPrivate] = useState(skill?.allow_private ?? true);
  const [defaultActive, setDefaultActive] = useState(skill?.default_active ?? true);
  const [enabled, setEnabled] = useState(skill?.enabled ?? true);
  const [importOpen, setImportOpen] = useState(defaultImportOpen);
  const [importText, setImportText] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const create = useMutation({
    ...createCustomSkillMutation(),
    onSuccess: () => {
      toast.success("已创建自定义 Skill 并热更新");
      onClose();
    },
  });

  const update = useMutation({
    ...updateCustomSkillMutation(),
    onSuccess: () => {
      toast.success("已保存自定义 Skill 并热更新");
      onClose();
    },
  });

  const applyImport = (raw: string) => {
    const parsed = parseSkillMarkdown(raw);
    if (!isEdit && parsed.id && !id) setId(parsed.id);
    if (parsed.name) setName(parsed.name);
    if (parsed.description) setDescription(parsed.description);
    if (parsed.instructions) setInstructions(parsed.instructions);
    toast.success("已从 SKILL.md 解析填充字段");
  };

  const submit = () => {
    if (isEdit) {
      update.mutate({
        path: { skill_id: skill.id },
        body: {
          name,
          description,
          instructions,
          defer_loading: deferLoading,
          handlers,
          allow_private: allowPrivate,
          default_active: defaultActive,
          enabled,
        },
      });
    } else {
      create.mutate({
        body: {
          id,
          name,
          description,
          instructions,
          defer_loading: deferLoading,
          handlers,
          allow_private: allowPrivate,
          default_active: defaultActive,
          enabled,
        },
      });
    }
  };

  const canSubmit = Boolean(
    (isEdit || id.trim()) && name.trim() && description.trim() && instructions.trim() && handlers.length > 0,
  );

  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={isEdit ? `编辑 Skill：${skill.name}` : "新建自定义 Skill"}
      description="自定义技能作为 pydantic-ai Capability 注入对话：支持按需加载（省 Token）或每次常驻注入。"
      onSubmit={submit}
      pending={create.isPending || update.isPending}
      submitDisabled={!canSubmit}
      className="sm:max-w-2xl"
    >
      <div className="rounded-lg border bg-muted/15 p-3">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-medium text-muted-foreground">从 SKILL.md 导入（支持粘贴或选择文件）</span>
          <Button type="button" size="xs" variant="ghost" onClick={() => setImportOpen((o) => !o)}>
            {importOpen ? "收起" : "展开导入"}
          </Button>
        </div>
        {importOpen && (
          <div className="mt-2 grid gap-2">
            <input
              ref={fileRef}
              type="file"
              accept=".md,.markdown,.txt"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (!f) return;
                void f.text().then((txt) => {
                  setImportText(txt);
                  applyImport(txt);
                });
              }}
            />
            <Textarea
              rows={5}
              className="font-mono text-xs"
              placeholder={"---\nname: server-faq\ndescription: 当群友询问服务器规则、入服流程或常见问题时加载\n---\n\n# 社区规范\n..."}
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
            />
            <div className="flex gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={!importText.trim()}
                onClick={() => applyImport(importText)}
              >
                解析并填入下方表单
              </Button>
              <Button type="button" size="sm" variant="ghost" onClick={() => fileRef.current?.click()}>
                <UploadIcon data-icon="inline-start" /> 选择本地 .md 文件
              </Button>
            </div>
          </div>
        )}
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field
          label="Skill ID"
          hint={isEdit ? "创建后不可修改" : "小写字母/数字/-/_，如 server-faq（不可与 kook / stickers 重名）"}
        >
          <Input
            required
            disabled={isEdit}
            className="font-mono text-xs"
            placeholder="server-faq"
            value={id}
            onChange={(e) => setId(e.target.value)}
          />
        </Field>
        <Field label="显示名称" hint="展示在控制台中的可读名称">
          <Input required placeholder="服务器常见问题" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>

      <Field
        label="触发说明（description）"
        hint="按需加载模式下会暴露给模型，写清「什么情况下应该加载本技能」。"
      >
        <Textarea
          required
          rows={2}
          placeholder="当用户询问服务器规则、活动日程或新人指引时加载本技能。"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </Field>

      <Field
        label={`技能正文（instructions · ${instructions.length} 字）`}
        hint="支持 Markdown、步骤清单、格式模板或领域知识；加载后会作为模型指令生效。"
      >
        <Textarea
          required
          rows={10}
          className="font-mono text-xs"
          placeholder="写下完整的执行规范、回答模板或知识库要点…"
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
        />
      </Field>

      <div className="grid gap-3 rounded-lg border bg-muted/20 p-3">
        <Field label="适用场景" hint="选择哪些处理器可以加载该技能">
          <MultiToggle
            options={["chat", "tldr"]}
            value={handlers}
            onChange={(v) => setHandlers(v.filter((x): x is "chat" | "tldr" => x === "chat" || x === "tldr"))}
          />
        </Field>
        <Field
          label="按需加载（defer_loading）"
          hint="开启后模型仅在需要时调用 load_capability 读取正文，节省 Token；关闭则每次对话常驻注入。"
          orientation="horizontal"
        >
          <Switch checked={deferLoading} onCheckedChange={setDeferLoading} />
        </Field>
        <Field
          label="全局默认生效"
          hint="开启后无需再到各频道手动添加；关闭后仅在「服务器与频道」的 tools.skills 显式包含本 Skill ID 时生效。"
          orientation="horizontal"
        >
          <Switch checked={defaultActive} onCheckedChange={setDefaultActive} />
        </Field>
        <Field label="允许在私聊中加载" orientation="horizontal">
          <Switch checked={allowPrivate} onCheckedChange={setAllowPrivate} />
        </Field>
        <Field label="启用该 Skill" orientation="horizontal">
          <Switch checked={enabled} onCheckedChange={setEnabled} />
        </Field>
      </div>
    </FormDialog>
  );
}
