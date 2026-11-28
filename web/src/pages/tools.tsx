import { useMutation, useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "@tanstack/react-router";
import {
  ArrowUpRightIcon,
  CheckCircle2Icon,
  CopyIcon,
  FileUpIcon,
  KeyRoundIcon,
  PencilIcon,
  PlusIcon,
  RadioIcon,
  RotateCcwIcon,
  SearchIcon,
  ServerIcon,
  Settings2Icon,
  ShieldIcon,
  SparklesIcon,
  UploadIcon,
  WrenchIcon,
  ZapIcon,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  createCustomSkillMutation,
  createGmodServerMutation,
  deleteCustomSkillMutation,
  deleteGmodServerMutation,
  getOpsToolsOverviewOptions,
  getScopeSettingsOptions,
  getSkillsOverviewOptions,
  getToolsOverviewOptions,
  listGmodServersOptions,
  listGuildsOptions,
  listKookGuildRolesOptions,
  listProvidersOptions,
  pingGmodServerMutation,
  putScopeSettingsMutation,
  testAzuriomToolMutation,
  testProvenanceToolMutation,
  updateBuiltinSkillMutation,
  updateCustomSkillMutation,
  updateGeneralToolPolicyMutation,
  updateGmodServerMutation,
  updateOpsToolsConfigMutation,
  updateToolConfigMutation,
} from "@/client/@tanstack/react-query.gen";
import type {
  BuiltinSkillOut,
  CustomSkillOut,
  GModBridgeOverviewOut,
  GModServerOut,
  OpsRoleRule,
  OpsToolOut,
  OpsToolsOverviewOut,
  ProviderOut,
  ToolItemOut,
  ToolParamFieldOut,
  ToolSecretIn,
} from "@/client/types.gen";
import {
  ApiKeyField,
  ConfirmButton,
  Empty,
  Field,
  FilterBar,
  FormDialog,
  GuildSelect,
  JsonEditor,
  Meta,
  MultiToggle,
  NumberInput,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
  SectionHeader,
  TagInput,
} from "@/components/common";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { fmtTime, isPlainObject } from "@/lib/format";
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
  { value: "gmod", label: "GMod 游戏服" },
  { value: "azuriom", label: "Azuriom 网站" },
];

const CATEGORY_LABELS: Record<string, string> = Object.fromEntries(
  CATEGORY_META.filter((c) => c.value !== "all").map((c) => [c.value, c.label]),
);

const OPS_CATEGORY_LABELS: Record<string, string> = {
  read: "只读查询",
  config: "配置与频道管理",
  moderation: "审核与消息处理",
  kook: "KOOK API",
  gmod: "GMod 服务器",
  azuriom: "Azuriom 网站",
};

function statusBadgeVariant(status: ToolStatus): "default" | "secondary" | "destructive" | "outline" {
  if (status === "ready") return "secondary";
  if (status === "warning") return "outline";
  if (status === "unconfigured") return "destructive";
  return "outline";
}

function formatFieldChip(field: ToolParamFieldOut, value: unknown): string {
  if (field.type === "boolean") {
    return `${field.label}${value ? "开" : "关"}`;
  }
  const unit = field.unit ?? "";
  return `${field.label} ${String(value ?? "")}${unit}`;
}

/** Dynamically summarize a tool's active parameters from backend-provided `param_fields`. */
function summarizeParams(tool: ToolItemOut): string[] {
  const fields = tool.param_fields ?? [];
  const params = tool.params ?? {};
  const out: string[] = [];

  for (const field of fields.slice(0, 4)) {
    if (field.key in params) {
      out.push(formatFieldChip(field, params[field.key]));
    }
  }
  if (fields.length > 4) {
    out.push(`共 ${fields.length} 项参数`);
  }
  if (tool.secret?.enabled && tool.secret.api_key_masked) {
    out.push(`独立 Key ${tool.secret.api_key_masked}`);
  }
  if (tool.slow) {
    out.push("计入耗时");
  }
  return out;
}

export function ToolsPage() {
  const hash = useLocation({ select: (location) => location.hash });
  const toolsQ = useQuery(getToolsOverviewOptions());
  const skillsQ = useQuery(getSkillsOverviewOptions());
  const opsQ = useQuery(getOpsToolsOverviewOptions());
  const gmodQ = useQuery({ ...listGmodServersOptions(), refetchInterval: 15_000 });
  const providersQ = useQuery(listProvidersOptions());

  return (
    <div className="max-w-6xl">
      <PageHeader
        title="工具与技能"
        description={
          <>
            集中配置每个工具自身的参数与独立加密凭据、管理内置及自定义 Skills，并配置管理助手（ops）的工具与角色授权。普通对话频道级工具权限请在
            <Link to="/channels" className="mx-1 text-primary underline-offset-4 hover:underline">
              服务器与频道
            </Link>
            中设置。
          </>
        }
      />

      <Tabs defaultValue={hash === "ops" ? "ops" : "tools"}>
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
          <TabsTrigger value="ops" className="gap-1.5">
            <ShieldIcon className="size-3.5" aria-hidden />
            管理助手 (ops)
            {opsQ.data && <Badge variant="secondary" className="ml-1 text-[11px]">{opsQ.data.tools.length}</Badge>}
          </TabsTrigger>
          <TabsTrigger value="gmod" className="gap-1.5">
            <RadioIcon className="size-3.5" aria-hidden />
            GMod 通信
            {gmodQ.data && <Badge variant="secondary" className="ml-1 text-[11px]">{gmodQ.data.servers.length}</Badge>}
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

        <TabsContent value="ops">
          <OpsTab data={opsQ.data} isLoading={opsQ.isLoading} error={opsQ.error} />
        </TabsContent>

        <TabsContent value="gmod">
          <GModBridgeTab data={gmodQ.data} isLoading={gmodQ.isLoading} error={gmodQ.error} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

// --- Shared Metadata-Driven Param Field Renderer -------------------------------------

function MetaParamField({
  field,
  value,
  defaultValue,
  onChange,
}: {
  field: ToolParamFieldOut;
  value: unknown;
  defaultValue: unknown;
  onChange: (v: unknown) => void;
}) {
  const hintParts: string[] = [];
  if (field.description) hintParts.push(field.description);
  const rangeParts: string[] = [];
  if (field.minimum != null) rangeParts.push(`≥ ${field.minimum}`);
  if (field.maximum != null) rangeParts.push(`≤ ${field.maximum}`);
  if (rangeParts.length) hintParts.push(`范围：${rangeParts.join("，")}`);
  if (defaultValue !== undefined) {
    const defText =
      typeof defaultValue === "boolean"
        ? defaultValue
          ? "开启"
          : "关闭"
        : `${defaultValue}${field.unit ?? ""}`;
    hintParts.push(`默认：${defText}`);
  }
  const hint = hintParts.join(" · ");
  const title = field.unit ? `${field.label}（${field.unit}）` : field.label;

  if (field.type === "boolean") {
    return (
      <Field label={title} hint={hint} orientation="horizontal">
        <Switch checked={Boolean(value)} onCheckedChange={onChange} />
      </Field>
    );
  }
  if (field.type === "enum" && field.options?.length) {
    return (
      <Field label={title} hint={hint}>
        <OptionSelect
          value={value == null ? null : String(value)}
          onChange={(v) => v !== null && onChange(v)}
          options={field.options.map((o) => ({ value: o, label: o }))}
        />
      </Field>
    );
  }
  if (field.type === "integer" || field.type === "number") {
    return (
      <Field label={title} hint={hint}>
        <NumberInput
          className="max-w-48"
          step={field.type === "integer" ? 1 : "any"}
          value={typeof value === "number" ? value : null}
          onChange={(v) => v !== null && onChange(v)}
        />
      </Field>
    );
  }
  return (
    <Field label={title} hint={hint}>
      <Input value={typeof value === "string" ? value : ""} onChange={(e) => onChange(e.target.value)} />
    </Field>
  );
}

function ParamFieldsView({
  fields,
  values,
  defaults,
  onChange,
}: {
  fields: ToolParamFieldOut[];
  values: Record<string, unknown>;
  defaults: Record<string, unknown>;
  onChange: (key: string, val: unknown) => void;
}) {
  if (!fields.length) return null;
  const groups = [...new Set(fields.map((f) => f.group || ""))];
  const hasMultipleGroups = groups.length > 1 && groups.every((g) => g.length > 0);
  if (hasMultipleGroups) {
    return (
      <Tabs defaultValue={groups[0]}>
        <TabsList className="flex w-full flex-wrap">
          {groups.map((g) => (
            <TabsTrigger key={g} value={g} className="flex-1 text-xs">
              {g}
            </TabsTrigger>
          ))}
        </TabsList>
        {groups.map((g) => (
          <TabsContent key={g} value={g} className="grid gap-3 pt-2">
            {fields
              .filter((f) => f.group === g)
              .map((f) => (
                <MetaParamField
                  key={f.key}
                  field={f}
                  value={values[f.key]}
                  defaultValue={defaults[f.key] ?? f.default}
                  onChange={(v) => onChange(f.key, v)}
                />
              ))}
          </TabsContent>
        ))}
      </Tabs>
    );
  }
  return (
    <div className="grid gap-3">
      {fields.map((f) => (
        <MetaParamField
          key={f.key}
          field={f}
          value={values[f.key]}
          defaultValue={defaults[f.key] ?? f.default}
          onChange={(v) => onChange(f.key, v)}
        />
      ))}
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
  data:
    | {
        items: ToolItemOut[];
        general: { request_limit: number; tool_calls_limit: number; slow_tools: string[] };
        runtime_flags: Record<string, boolean>;
      }
    | undefined;
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

  const testAzuriom = useMutation({
    ...testAzuriomToolMutation(),
    onSuccess: (result) => {
      if (!result.ok) {
        toast.error(result.error || "Ygg Bridge 连接失败");
        return;
      }
      const ready = result.kook_accounts_ready;
      const kookStatus = ready == null ? "未知" : ready ? "就绪" : "未就绪";
      toast.success("Ygg Bridge 连接成功", {
        description: `延迟 ${result.latency_ms} ms · 插件版本 ${result.version || "未知"} · kook_accounts ${kookStatus}`,
      });
    },
    onError: (error) => toast.error(error instanceof Error ? error.message : "Ygg Bridge 测试请求失败"),
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
                      {tool.id === "azuriom_lookup" && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={testAzuriom.isPending}
                          onClick={() => testAzuriom.mutate({})}
                        >
                          <ZapIcon data-icon="inline-start" /> {testAzuriom.isPending ? "测试中…" : "测试连接"}
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

      {testingProvenance && <ProvenanceTestDialog onClose={() => setTestingProvenance(false)} />}
    </div>
  );
}

type SecretMode = "auto" | "provider" | "custom";

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

  const providerKey = tool.provider_param_key ?? null;
  const supportsSecret = Boolean(tool.supports_secret);
  const hasProviderMode = Boolean(supportsSecret && providerKey);

  const initialMode: SecretMode = hasProviderMode
    ? String(tool.params?.[providerKey!] ?? "").trim()
      ? "provider"
      : tool.secret?.enabled
        ? "custom"
        : "auto"
    : tool.secret?.enabled
      ? "custom"
      : "auto";

  const [secretMode, setSecretMode] = useState<SecretMode>(initialMode);
  const [providerRef, setProviderRef] = useState<string>(
    providerKey ? String(tool.params?.[providerKey] ?? "") : "",
  );
  const [secEnabled, setSecEnabled] = useState<boolean>(Boolean(tool.secret?.enabled));
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
    if (hasProviderMode) {
      setSecretMode("auto");
      setProviderRef("");
    }
  };

  const submit = () => {
    const bodyParams: Record<string, unknown> = { ...params };
    let secretPayload: ToolSecretIn | null = null;
    if (supportsSecret) {
      if (hasProviderMode && providerKey) {
        bodyParams[providerKey] = secretMode === "provider" ? providerRef.trim() : "";
        secretPayload = {
          enabled: secretMode === "custom",
          base_url: secBaseUrl.trim(),
          api_key: secApiKey.trim() || null,
          clear_api_key: secClearKey,
          headers: secHeaders,
        };
      } else {
        secretPayload = {
          enabled: secEnabled,
          base_url: secBaseUrl.trim(),
          api_key: secApiKey.trim() || null,
          clear_api_key: secClearKey,
          headers: secHeaders,
        };
      }
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

  const fields = tool.param_fields ?? [];
  const hasMultipleGroups = new Set(fields.map((f) => f.group).filter(Boolean)).size > 1;
  const hasParams = fields.length > 0 || supportsSecret;
  const isCustomSecretActive = hasProviderMode ? secretMode === "custom" : secEnabled;

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
      submitDisabled={
        (isCustomSecretActive && !headersValid) || (hasProviderMode && secretMode === "provider" && !providerRef.trim())
      }
      className={hasMultipleGroups || supportsSecret ? "sm:max-w-2xl" : "sm:max-w-lg"}
    >
      <div className="grid gap-3 rounded-lg border bg-muted/20 p-3">
        <Field
          label="全局默认启用"
          hint="全局默认开关；若想「仅允许部分 KOOK 角色调用」，关闭此处后前往「服务器与频道」→ 覆盖 → access.role_rules 的 tools_allow 为指定角色放行。"
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

      {supportsSecret && (
        <div className="grid gap-4 rounded-lg border p-3">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium">连接与加密凭据</span>
            <Badge variant={statusBadgeVariant(tool.status)}>{tool.status_label}</Badge>
          </div>

          {hasProviderMode ? (
            <>
              <Field
                label="凭据来源模式"
                hint={
                  tool.id === "check_provenance"
                    ? runtimeFlags.openai_env_key_configured
                      ? "自动模式优先匹配指向 api.openai.com 的已启用 Provider，否则回退至环境变量 OPENAI_API_KEY（当前已配置）。"
                      : "自动模式优先匹配指向 api.openai.com 的已启用 Provider（当前未设置环境变量 OPENAI_API_KEY）。"
                    : "选择自动匹配、复用已有 Provider，或单独配置本工具的加密凭据。"
                }
              >
                <OptionSelect
                  value={secretMode}
                  onChange={(v) => setSecretMode((v as SecretMode) ?? "auto")}
                  options={[
                    { value: "auto", label: "自动匹配（默认规则 / 环境变量）" },
                    { value: "provider", label: "复用已有 Provider（从控制台 Providers 选择）" },
                    { value: "custom", label: "独立加密凭据（单独配置 Base URL 与 API Key）" },
                  ]}
                />
              </Field>
              {secretMode === "provider" && (
                <Field label="选择提供商">
                  {providerOptions.length > 0 ? (
                    <OptionSelect
                      value={providerRef || null}
                      onChange={(v) => setProviderRef(v ?? "")}
                      placeholder="选择一个已配置的 Provider"
                      options={providerOptions}
                    />
                  ) : (
                    <Input
                      placeholder="输入 Provider 名称或先在 Providers 页面添加"
                      value={providerRef}
                      onChange={(e) => setProviderRef(e.target.value)}
                    />
                  )}
                </Field>
              )}
            </>
          ) : (
            <Field label="启用独立加密凭据" orientation="horizontal">
              <Switch checked={secEnabled} onCheckedChange={setSecEnabled} />
            </Field>
          )}

          {isCustomSecretActive && (
            <div className="grid gap-3 rounded-md border bg-muted/10 p-3">
              <Field
                label="Base URL"
                hint={tool.secret_hint || (tool.secret_default_base_url ? `留空默认使用 ${tool.secret_default_base_url}` : "可选，填完整 https:// 地址")}
              >
                <Input
                  placeholder={tool.secret_default_base_url || "https://api.example.com/v1"}
                  value={secBaseUrl}
                  onChange={(e) => setSecBaseUrl(e.target.value)}
                />
              </Field>
              <ApiKeyField
                masked={tool.secret?.api_key_masked}
                value={secApiKey}
                clear={secClearKey}
                emptyHint="加密存储，只展示末四位"
                onChange={(patch) => {
                  if (patch.api_key !== undefined) setSecApiKey(patch.api_key);
                  if (patch.clear_api_key !== undefined) setSecClearKey(patch.clear_api_key);
                }}
              />
              <Field label="附加请求头（JSON，可选）" hint='例如 {"X-Custom-Header": "value"}'>
                <JsonEditor
                  rows={3}
                  value={secHeaders}
                  onValidityChange={setHeadersValid}
                  onChange={(v) => isPlainObject(v) && setSecHeaders(v as Record<string, string>)}
                />
              </Field>
            </div>
          )}
        </div>
      )}

      <ParamFieldsView
        fields={fields}
        values={params}
        defaults={tool.defaults ?? {}}
        onChange={setParam}
      />

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
                  {(skill.param_fields ?? []).map((f) => (
                    <Badge key={f.key} variant="outline" className="text-[11px] font-normal text-muted-foreground">
                      {formatFieldChip(f, skill.params?.[f.key])}
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
      <ParamFieldsView
        fields={skill.param_fields ?? []}
        values={params}
        defaults={skill.defaults ?? {}}
        onChange={(k, v) => setParams((cur) => ({ ...cur, [k]: v }))}
      />
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

// --- Tab 3: Ops Agent Tools & Role Authorization -------------------------------------

function OpsTab({
  data,
  isLoading,
  error,
}: {
  data: OpsToolsOverviewOut | undefined;
  isLoading: boolean;
  error: unknown;
}) {
  const guildsQ = useQuery(listGuildsOptions());
  const [scopeGuildId, setScopeGuildId] = useState<string | null>(null);
  const scopeSettingsQ = useQuery({
    ...getScopeSettingsOptions({ path: { scope: "guild", scope_id: scopeGuildId ?? "" } }),
    enabled: Boolean(scopeGuildId),
  });
  const liveRolesQ = useQuery({
    ...listKookGuildRolesOptions({ path: { guild_id: scopeGuildId ?? "" } }),
    enabled: Boolean(scopeGuildId),
    retry: false,
  });

  const [writeActions, setWriteActions] = useState<string[]>([]);
  const [editingRule, setEditingRule] = useState<{ roleId: string; rule: OpsRoleRule; isNew: boolean } | null>(null);

  useEffect(() => {
    if (data) setWriteActions(data.kook_write_actions);
  }, [data?.kook_write_actions]);

  const saveGlobalOps = useMutation({
    ...updateOpsToolsConfigMutation(),
    onSuccess: () => toast.success("已保存管理助手配置并热更新"),
  });

  const saveGuildScope = useMutation({
    ...putScopeSettingsMutation(),
    onSuccess: () => toast.success("已保存该服务器的管理助手角色授权"),
  });

  if (!data) {
    return <QueryState isLoading={isLoading} error={error} variant="card" />;
  }

  const actionsDirty = JSON.stringify(writeActions) !== JSON.stringify(data.kook_write_actions);

  const guildRoleRulesRaw = scopeGuildId
    ? ((scopeSettingsQ.data?.value as Record<string, unknown> | undefined)?.ops as Record<string, unknown> | undefined)
        ?.role_rules
    : undefined;
  const activeRoleRules: Record<string, OpsRoleRule> = scopeGuildId
    ? isPlainObject(guildRoleRulesRaw)
      ? (guildRoleRulesRaw as Record<string, OpsRoleRule>)
      : {}
    : data.role_rules;

  const roleNameMap = new Map((liveRolesQ.data ?? []).map((r) => [r.role_id, r.name]));
  const toolNameMap = new Map(data.tools.map((t) => [t.id, t.name]));

  const persistRoleRules = (nextRules: Record<string, OpsRoleRule>, onDone?: () => void) => {
    if (!scopeGuildId) {
      saveGlobalOps.mutate(
        { body: { role_rules: nextRules } },
        { onSuccess: () => onDone?.() },
      );
      return;
    }
    const existingDoc = isPlainObject(scopeSettingsQ.data?.value) ? { ...scopeSettingsQ.data.value } : {};
    const existingOps = isPlainObject(existingDoc.ops) ? { ...existingDoc.ops } : {};
    if (Object.keys(nextRules).length > 0) {
      existingOps.role_rules = nextRules;
      existingDoc.ops = existingOps;
    } else {
      delete existingOps.role_rules;
      if (Object.keys(existingOps).length > 0) existingDoc.ops = existingOps;
      else delete existingDoc.ops;
    }
    saveGuildScope.mutate(
      { path: { scope: "guild", scope_id: scopeGuildId }, body: existingDoc },
      { onSuccess: () => onDone?.() },
    );
  };

  return (
    <div className="grid gap-8">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="text-base">KOOK API 写接口全局白名单（ops.kook_write_actions）</CardTitle>
              <CardDescription className="mt-1 text-xs">
                管理助手通过 <code className="font-mono">kook_api</code> 调用 GET 只读接口默认放行；POST 写接口仅限此处列出的路径，且角色授权不能超过此白名单。
                高危禁区（{data.never_allowed.join("、")}）始终硬编码禁止。
              </CardDescription>
            </div>
            <PendingButton
              size="sm"
              pending={saveGlobalOps.isPending}
              disabled={!actionsDirty}
              onClick={() => saveGlobalOps.mutate({ body: { kook_write_actions: writeActions } })}
            >
              {actionsDirty ? "保存白名单" : "已同步"}
            </PendingButton>
          </div>
        </CardHeader>
        <CardContent>
          <Field label="允许的 POST 接口路径" hint="输入以 / 开头的 KOOK API v3 路径后回车添加，如 /message/pin、/guild-role/grant">
            <TagInput value={writeActions} onChange={setWriteActions} placeholder="例如 /message/pin" />
          </Field>
        </CardContent>
      </Card>

      <section className="grid gap-3">
        <SectionHeader
          title="按 KOOK 角色授权管理助手（ops）工具"
          description="超级管理员始终拥有全部 ops 工具与全局权限。你可以为指定 KOOK 角色放行部分 ops 工具（默认开启同服隔离，禁止越权修改全局、其他服务器或 ops 权限本身）。"
          actions={
            <div className="flex flex-wrap items-center gap-2">
              <OptionSelect
                aria-label="选择配置范围"
                className="w-56"
                value={scopeGuildId}
                onChange={setScopeGuildId}
                noneLabel="全局默认规则"
                options={(guildsQ.data ?? []).map((g) => ({
                  value: g.guild_id,
                  label: `服务器：${g.name || g.guild_id}`,
                }))}
              />
              <Button
                size="sm"
                disabled={Boolean(scopeGuildId && !scopeSettingsQ.data)}
                onClick={() =>
                  setEditingRule({
                    roleId: "",
                    rule: { tools: ["status", "list_channels"], kook_write_actions: [], guild_only: true },
                    isNew: true,
                  })
                }
              >
                <PlusIcon data-icon="inline-start" /> 添加角色授权
              </Button>
            </div>
          }
        />

        {scopeGuildId && !scopeSettingsQ.data ? (
          <QueryState isLoading={scopeSettingsQ.isLoading} error={scopeSettingsQ.error} variant="card" />
        ) : Object.keys(activeRoleRules).length === 0 ? (
          <Empty
            icon={<ShieldIcon />}
            description={
              scopeGuildId
                ? "当前服务器尚未单独覆盖 ops 角色授权（将继承全局默认规则）。点击右上角可按本服 KOOK 角色直接勾选。"
                : "目前仅超级管理员可使用 @bot ops 指令。如需允许部分 KOOK 角色使用部分 ops 工具，可点击右上角添加授权。"
            }
          >
            {scopeGuildId ? "本服务器暂无单独的 ops 角色授权" : "暂无全局 ops 角色授权"}
          </Empty>
        ) : (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {Object.entries(activeRoleRules).map(([roleId, rule]) => {
              const roleName = roleNameMap.get(roleId);
              const tools = rule.tools ?? [];
              const writes = rule.kook_write_actions ?? [];
              const guildOnly = rule.guild_only ?? true;
              return (
                <Card key={roleId} className="flex flex-col">
                  <CardHeader className="pb-3">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <CardTitle className="text-base">{roleName || `角色 ${roleId}`}</CardTitle>
                          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                            {roleId}
                          </code>
                        </div>
                      </div>
                      <Badge variant={guildOnly ? "secondary" : "destructive"} className="text-[11px]">
                        {guildOnly ? "仅限本服务器" : "允许跨服/全局"}
                      </Badge>
                    </div>
                  </CardHeader>
                  <CardContent className="grid flex-1 gap-2 pt-0">
                    <div>
                      <div className="mb-1 text-xs text-muted-foreground">已授权 ops 工具（{tools.length}）：</div>
                      <div className="flex flex-wrap gap-1">
                        {tools.map((tid) => (
                          <Badge key={tid} variant="outline" className="text-[11px] font-normal">
                            {toolNameMap.get(tid) ?? tid}
                          </Badge>
                        ))}
                      </div>
                    </div>
                    {tools.includes("kook_api") && (
                      <div>
                        <div className="mb-1 text-xs text-muted-foreground">
                          kook_api 写接口：
                        </div>
                        {writes.length > 0 ? (
                          <div className="flex flex-wrap gap-1">
                            {writes.map((w) => (
                              <Badge key={w} variant="secondary" className="font-mono text-[11px] font-normal">
                                {w}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-xs text-muted-foreground">仅 GET 只读（未授写接口）</span>
                        )}
                      </div>
                    )}
                  </CardContent>
                  <CardFooter className="flex justify-end gap-1.5 border-t pt-3">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setEditingRule({ roleId, rule, isNew: false })}
                    >
                      <PencilIcon data-icon="inline-start" /> 编辑
                    </Button>
                    <ConfirmButton
                      title={`移除角色 ${roleName || roleId} 的 ops 授权？`}
                      onConfirm={() => {
                        const { [roleId]: _, ...rest } = activeRoleRules;
                        persistRoleRules(rest);
                      }}
                    >
                      删除
                    </ConfirmButton>
                  </CardFooter>
                </Card>
              );
            })}
          </div>
        )}
      </section>

      <section className="grid gap-3">
        <SectionHeader
          title="已注册的 ops 管理工具一览"
          description="来自后端 OPS_TOOL_SPECS 注册表；新增管理助手工具后会自动出现在此列表和上方角色授权勾选框中。"
        />
        <div className="grid grid-cols-1 gap-3 md:grid-cols-2 lg:grid-cols-3">
          {data.tools.map((t) => (
            <div key={t.id} className="rounded-lg border p-3">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-sm font-medium">{t.name}</span>
                <code className="rounded bg-muted px-1 py-0.5 font-mono text-[11px] text-muted-foreground">{t.id}</code>
                <Badge variant={t.write ? "default" : "secondary"} className="ml-auto text-[10px]">
                  {t.write ? "写" : "读"}
                </Badge>
                {t.global_only && (
                  <Badge variant="outline" className="text-[10px]">
                    仅全局
                  </Badge>
                )}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">{t.description}</p>
            </div>
          ))}
        </div>
      </section>

      {editingRule && (
        <OpsRoleRuleDialog
          initial={editingRule}
          allTools={data.tools}
          globalWriteActions={data.kook_write_actions}
          scopeGuildId={scopeGuildId}
          existingRoleIds={Object.keys(activeRoleRules)}
          pending={saveGlobalOps.isPending || saveGuildScope.isPending}
          onSave={(roleIds, rule) => {
            const next = { ...activeRoleRules };
            for (const id of roleIds) {
              if (!editingRule.isNew || !Object.hasOwn(next, id)) next[id] = rule;
            }
            persistRoleRules(next, () => setEditingRule(null));
          }}
          onClose={() => setEditingRule(null)}
        />
      )}
    </div>
  );
}

function OpsRoleRuleDialog({
  initial,
  allTools,
  globalWriteActions,
  scopeGuildId,
  existingRoleIds,
  pending,
  onSave,
  onClose,
}: {
  initial: { roleId: string; rule: OpsRoleRule; isNew: boolean };
  allTools: OpsToolOut[];
  globalWriteActions: string[];
  scopeGuildId: string | null;
  existingRoleIds: string[];
  pending: boolean;
  onSave: (roleIds: string[], rule: OpsRoleRule) => void;
  onClose: () => void;
}) {
  const [roleText, setRoleText] = useState(initial.roleId);
  const [sourceGuildId, setSourceGuildId] = useState(scopeGuildId);
  const rolesQ = useQuery({
    ...listKookGuildRolesOptions({ path: { guild_id: sourceGuildId ?? "" } }),
    enabled: initial.isNew && Boolean(sourceGuildId),
    retry: false,
  });
  const roleIds = [...new Set(roleText.split(/[\s,，]+/).filter(Boolean))];
  const targets = initial.isNew ? roleIds.filter((id) => !existingRoleIds.includes(id)) : [initial.roleId];
  const invalidIds = initial.isNew && targets.some((id) => !/^\d+$/.test(id));
  const [tools, setTools] = useState<OpsToolOut["id"][]>(initial.rule.tools ?? []);
  const [writes, setWrites] = useState<string[]>(initial.rule.kook_write_actions ?? []);
  const [guildOnly, setGuildOnly] = useState<boolean>(initial.rule.guild_only ?? true);

  const toggleTool = (id: OpsToolOut["id"], checked: boolean) => {
    setTools((cur) => (checked ? (cur.includes(id) ? cur : [...cur, id]) : cur.filter((x) => x !== id)));
  };

  const categories = [...new Set(allTools.map((t) => t.category))];
  const cleanTools = guildOnly ? tools.filter((id) => !allTools.find((t) => t.id === id)?.global_only) : tools;

  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={initial.isNew ? "添加 ops 角色授权" : `编辑角色 ${initial.roleId} 的 ops 授权`}
      description="被授权的角色在频道中 @bot ops <指令> 即可调用以下选中的管理工具。"
      onSubmit={() => {
        onSave(targets, {
          tools: cleanTools,
          kook_write_actions: cleanTools.includes("kook_api") ? writes : [],
          guild_only: guildOnly,
        });
      }}
      pending={pending}
      submitLabel={initial.isNew && targets.length ? `授权给 ${targets.length} 个角色` : "保存"}
      submitDisabled={!targets.length || invalidIds || !cleanTools.length}
      className="sm:max-w-2xl"
    >
      {initial.isNew && !scopeGuildId && (
        <Field label="从服务器选择角色" hint="授权将保存到全局默认规则。">
          <GuildSelect value={sourceGuildId} onChange={setSourceGuildId} allLabel="手动输入角色 ID" />
        </Field>
      )}
      {initial.isNew && sourceGuildId && (
        <div role="group" aria-label="选择 KOOK 角色" className="grid max-h-40 gap-1 overflow-y-auto rounded-lg border p-2">
          {rolesQ.data?.length ? rolesQ.data.map((r) => {
            const existing = existingRoleIds.includes(r.role_id);
            return (
              <label key={r.role_id} className="flex items-center gap-2 px-1 py-0.5 text-sm">
                <Checkbox
                  disabled={existing}
                  checked={!existing && roleIds.includes(r.role_id)}
                  onCheckedChange={(checked) => setRoleText(
                    (checked ? [...roleIds, r.role_id] : roleIds.filter((id) => id !== r.role_id)).join(", ")
                  )}
                />
                {r.name} <span className="text-xs text-muted-foreground">{r.role_id}{existing && " · 已配置"}</span>
              </label>
            );
          }) : <span className="text-xs text-muted-foreground">
            {rolesQ.isLoading ? "正在获取角色…" : rolesQ.isError ? "读取角色失败，可手动输入角色 ID。" : "该服暂无角色，可手动输入角色 ID。"}
          </span>}
        </div>
      )}
      <Field
        label="KOOK 角色 ID"
        hint={initial.isNew ? "支持多个 ID，用逗号或空格分隔。已有授权的角色会跳过。" : undefined}
        error={invalidIds ? "角色 ID 必须是数字。" : undefined}
      >
        <Input disabled={!initial.isNew} className="font-mono text-xs" placeholder="例如 48796009"
          value={roleText} onChange={(e) => setRoleText(e.target.value)} />
      </Field>

      <Field
        label="仅限操作当前所在服务器（guild_only）"
        hint="强烈建议保持开启：强制校验所有读写只能作用在触发所在服务器内，禁止改全局配置、其他服务器或修改 ops 权限本身。"
        orientation="horizontal"
      >
        <Switch checked={guildOnly} onCheckedChange={setGuildOnly} />
      </Field>

      <div className="grid gap-3">
        {categories.map((cat) => {
          const items = allTools.filter((t) => t.category === cat);
          if (!items.length) return null;
          return (
            <div key={cat} className="rounded-lg border p-3">
              <div className="mb-2 text-xs font-medium text-muted-foreground">
                {OPS_CATEGORY_LABELS[cat] ?? cat}
              </div>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {items.map((t) => {
                  const disabled = guildOnly && t.global_only;
                  const checked = !disabled && tools.includes(t.id);
                  return (
                    <label
                      key={t.id}
                      className={cn(
                        "flex cursor-pointer items-start gap-2 rounded-md border p-2 text-xs transition-colors",
                        checked && "border-primary/40 bg-primary/5",
                        disabled && "cursor-not-allowed opacity-50",
                      )}
                    >
                      <Checkbox
                        checked={checked}
                        disabled={disabled}
                        onCheckedChange={(v) => toggleTool(t.id, Boolean(v))}
                        className="mt-0.5"
                      />
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-1">
                          <span className="font-medium text-foreground">{t.name}</span>
                          <code className="font-mono text-[10px] text-muted-foreground">{t.id}</code>
                          {t.global_only && <Badge variant="outline" className="text-[10px]">仅全局</Badge>}
                        </div>
                        <p className="mt-0.5 text-[11px] text-muted-foreground">{t.description}</p>
                      </div>
                    </label>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>

      {tools.includes("kook_api") && (
        <Field
          label="允许该角色通过 kook_api 调用的写接口（可选）"
          hint="留空表示该角色仅可使用 GET 只读查询；只能从全局 ops.kook_write_actions 白名单里勾选子集。"
        >
          <MultiToggle options={globalWriteActions} value={writes} onChange={setWrites} />
        </Field>
      )}
    </FormDialog>
  );
}

// --- Tab 4: GMod Multi-Server Bridge (Pure Transport) --------------------------------

function GModBridgeTab({
  data,
  isLoading,
  error,
}: {
  data: GModBridgeOverviewOut | undefined;
  isLoading: boolean;
  error: unknown;
}) {
  const [editing, setEditing] = useState<GModServerOut | "new" | null>(null);
  const [revealed, setRevealed] = useState<{ serverId: string; token: string } | null>(null);
  const [pingResult, setPingResult] = useState<{ serverId: string; ok: boolean; latency_ms: number; data?: unknown; error?: string | null } | null>(null);

  const remove = useMutation({
    ...deleteGmodServerMutation(),
    onSuccess: () => toast.success("已删除 GMod 服务器配置"),
  });

  const ping = useMutation({
    ...pingGmodServerMutation(),
    onSuccess: (res, vars) => {
      setPingResult({ serverId: vars.path.server_id, ...res });
      if (res.ok) toast.success(`${vars.path.server_id} 探活成功（${res.latency_ms}ms）`);
      else toast.error(res.error ?? "Ping 失败");
    },
  });

  if (!data) {
    return <QueryState isLoading={isLoading} error={error} variant="card" />;
  }

  const copyText = (txt: string, label: string) => {
    void navigator.clipboard.writeText(txt).then(() => toast.success(`已复制${label}`));
  };

  return (
    <div className="grid gap-6">
      <Card>
        <CardHeader className="py-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <CardTitle className="text-base">GMod 长轮询通信桥接（多服）</CardTitle>
                <Badge variant={data.serving ? "secondary" : "outline"}>
                  {data.serving ? `监听中 :${data.bridge_port}` : data.bridge_port > 0 ? `端口 :${data.bridge_port}` : "已禁用"}
                </Badge>
              </div>
              <CardDescription className="mt-1 text-xs">
                同机翼龙容器走内网（例如 <code className="font-mono">http://172.18.0.1:{data.bridge_port || 8091}/bridge/gmod</code>），
                跨机服务器可通过 Caddy / Nginx 反代 <code className="font-mono">https://你的域名/bridge/gmod/* → bot:{data.bridge_port || 8091}</code> 走公网 HTTPS。
              </CardDescription>
            </div>
            <Button size="sm" onClick={() => setEditing("new")}>
              <PlusIcon data-icon="inline-start" /> 添加服务器
            </Button>
          </div>
        </CardHeader>
      </Card>

      {revealed && (
        <Alert>
          <KeyRoundIcon className="size-4" />
          <AlertTitle>新生成的 Token（仅展示这一次，请立即复制保存）</AlertTitle>
          <AlertDescription className="mt-2 grid gap-2">
            <pre className="rounded-md bg-muted p-2.5 font-mono text-xs whitespace-pre-wrap text-foreground">
              {[
                `ygg_bridge_url "http://172.18.0.1:${data.bridge_port || 8091}/bridge/gmod"  // 跨机请改为 https://你的域名/bridge/gmod`,
                `ygg_bridge_server_id "${revealed.serverId}"`,
                `ygg_bridge_token "${revealed.token}"`,
                `ygg_bridge_enabled "1"`,
              ].join("\n")}
            </pre>
            <div className="flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => copyText(revealed.token, "Token")}>
                <CopyIcon data-icon="inline-start" /> 复制 Token
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  copyText(
                    `ygg_bridge_url "http://172.18.0.1:${data.bridge_port || 8091}/bridge/gmod"\nygg_bridge_server_id "${revealed.serverId}"\nygg_bridge_token "${revealed.token}"\nygg_bridge_enabled "1"`,
                    "server.cfg 配置段",
                  )
                }
              >
                <CopyIcon data-icon="inline-start" /> 复制 server.cfg 配置段
              </Button>
              <Button size="sm" variant="ghost" onClick={() => setRevealed(null)}>
                我已保存，关闭提示
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}

      {pingResult && (
        <Alert variant={pingResult.ok ? "default" : "destructive"}>
          <AlertTitle>
            {pingResult.serverId} Ping {pingResult.ok ? `成功（${pingResult.latency_ms}ms）` : "失败"}
          </AlertTitle>
          <AlertDescription className="mt-1">
            {pingResult.ok ? (
              <pre className="rounded bg-muted p-2 font-mono text-xs whitespace-pre-wrap text-foreground">
                {JSON.stringify(pingResult.data, null, 2)}
              </pre>
            ) : (
              <span>{pingResult.error}</span>
            )}
          </AlertDescription>
        </Alert>
      )}

      {data.servers.length === 0 ? (
        <Empty
          icon={<ServerIcon />}
          description="添加服务器后会生成独立的加密 Bearer Token，将配置填入 GMod 端的 ygg_bridge 插件即可自动上线。"
          action={
            <Button size="sm" onClick={() => setEditing("new")}>
              <PlusIcon data-icon="inline-start" /> 添加第一台 GMod 服务器
            </Button>
          }
        >
          还没有配置 GMod 服务器
        </Empty>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {data.servers.map((srv) => (
            <Card key={srv.id} className={cn("flex flex-col", !srv.enabled && "opacity-70")}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <CardTitle className="text-base">{srv.name}</CardTitle>
                      <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                        {srv.id}
                      </code>
                      <Badge variant={srv.online ? "default" : "outline"} className="text-[11px]">
                        {srv.online ? "在线" : "离线"}
                      </Badge>
                      {!srv.enabled && (
                        <Badge variant="secondary" className="text-[11px]">
                          已停用
                        </Badge>
                      )}
                    </div>
                    {srv.note && <CardDescription className="mt-1 text-xs">{srv.note}</CardDescription>}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="grid flex-1 gap-2 pt-0 text-xs">
                <Meta
                  items={[
                    `Token: ${srv.token_masked ?? "—"}`,
                    srv.remote_ip ? `来源 IP: ${srv.remote_ip}` : null,
                    srv.last_seen ? `最近心跳: ${fmtTime(srv.last_seen)}` : "尚未连接",
                  ]}
                />
                {!!srv.handlers?.length && (
                  <div className="flex flex-wrap items-center gap-1">
                    <span className="text-muted-foreground">已注册动作：</span>
                    {srv.handlers.map((h) => (
                      <Badge key={h} variant="outline" className="font-mono text-[11px] font-normal">
                        {h}
                      </Badge>
                    ))}
                  </div>
                )}
              </CardContent>
              <CardFooter className="flex justify-end gap-1.5 border-t pt-3">
                <PendingButton
                  size="sm"
                  variant="outline"
                  pending={ping.isPending && ping.variables?.path.server_id === srv.id}
                  disabled={!srv.enabled}
                  onClick={() => ping.mutate({ path: { server_id: srv.id } })}
                >
                  <ZapIcon data-icon="inline-start" /> Ping
                </PendingButton>
                <Button size="sm" variant="outline" onClick={() => setEditing(srv)}>
                  <PencilIcon data-icon="inline-start" /> 编辑
                </Button>
                <ConfirmButton
                  title={`删除 GMod 服务器「${srv.name}」？`}
                  description="删除后该服务器将无法通过长轮询鉴权。"
                  onConfirm={() => remove.mutate({ path: { server_id: srv.id } })}
                >
                  删除
                </ConfirmButton>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}

      {editing && (
        <GModServerDialog
          server={editing === "new" ? null : editing}
          onCreated={(sid, token) => {
            setEditing(null);
            if (token) setRevealed({ serverId: sid, token });
          }}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

function GModServerDialog({
  server,
  onCreated,
  onClose,
}: {
  server: GModServerOut | null;
  onCreated: (serverId: string, token: string | null) => void;
  onClose: () => void;
}) {
  const isEdit = server !== null;
  const [id, setId] = useState(server?.id ?? "");
  const [name, setName] = useState(server?.name ?? "");
  const [note, setNote] = useState(server?.note ?? "");
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [token, setToken] = useState("");
  const [rotateToken, setRotateToken] = useState(false);

  const create = useMutation({
    ...createGmodServerMutation(),
    onSuccess: (res) => {
      toast.success("已添加 GMod 服务器");
      onCreated(res.server.id, res.plaintext_token ?? null);
    },
  });

  const update = useMutation({
    ...updateGmodServerMutation(),
    onSuccess: (res) => {
      toast.success("已更新 GMod 服务器");
      onCreated(res.server.id, res.plaintext_token ?? null);
    },
  });

  const submit = () => {
    if (isEdit) {
      update.mutate({
        path: { server_id: server.id },
        body: { name, note, enabled, token: token.trim() || null, rotate_token: rotateToken },
      });
    } else {
      create.mutate({
        body: { id: id.trim(), name, note, enabled, token: token.trim() || null },
      });
    }
  };

  return (
    <FormDialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={isEdit ? `编辑 GMod 服务器：${server.name}` : "添加 GMod 服务器"}
      description="每台 GMod 服务器使用独立 ID 与加密 Bearer Token，支持同机内网或跨机公网 HTTPS 接入。"
      onSubmit={submit}
      pending={create.isPending || update.isPending}
      submitDisabled={!(isEdit || id.trim()) || !name.trim()}
    >
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="服务器 ID" hint={isEdit ? "创建后不可修改" : "小写字母/数字/-/_，如 main、ttt-1"}>
          <Input
            required
            disabled={isEdit}
            className="font-mono text-xs"
            placeholder="main"
            value={id}
            onChange={(e) => setId(e.target.value)}
          />
        </Field>
        <Field label="显示名称">
          <Input required placeholder="主服 (Sandbox)" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
      </div>
      <Field label="备注（可选）" hint="例如「同机翼龙内网」或「香港节点公网 HTTPS」">
        <Input placeholder="同机翼龙内网" value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <Field
        label={isEdit ? "自定义替换 Token（留空保持不变）" : "自定义 Token（留空自动生成强随机密钥）"}
        hint={isEdit ? `当前 Token：${server.token_masked ?? "无"}` : undefined}
      >
        <Input
          type="password"
          placeholder={isEdit ? "留空保持原 Token" : "留空自动生成"}
          value={token}
          disabled={rotateToken}
          onChange={(e) => setToken(e.target.value)}
        />
      </Field>
      {isEdit && (
        <Field label="轮换并自动生成新 Token" hint="勾选后旧 Token 将立即失效，保存后弹出新 Token" orientation="horizontal">
          <Switch checked={rotateToken} onCheckedChange={setRotateToken} />
        </Field>
      )}
      <Field label="启用该服务器" orientation="horizontal">
        <Switch checked={enabled} onCheckedChange={setEnabled} />
      </Field>
    </FormDialog>
  );
}
