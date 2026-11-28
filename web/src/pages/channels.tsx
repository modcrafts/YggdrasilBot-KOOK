import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  ChevronsUpDownIcon,
  DownloadIcon,
  FolderIcon,
  HashIcon,
  MessagesSquareIcon,
  PlusIcon,
  RefreshCwIcon,
  ServerIcon,
  SlidersHorizontalIcon,
  Trash2Icon,
  XIcon,
} from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  backfillChannelMutation,
  deleteChannelMutation,
  deleteGuildMutation,
  getChannelTreeOptions,
  getScopeSettingsOptions,
  getSettingsSchemaOptions,
  listKookGuildRolesOptions,
  putScopeSettingsMutation,
  syncForumChannelThreadsMutation,
  syncKookStructureMutation,
  upsertChannelMutation,
  upsertGuildMutation,
} from "@/client/@tanstack/react-query.gen";
import type { ChannelOut, GuildOut, GuildTreeOut } from "@/client/types.gen";
import {
  ConfirmButton,
  Empty,
  Field,
  JsonEditor,
  OptionSelect,
  PageHeader,
  PendingButton,
  QueryState,
} from "@/components/common";
import {
  getAt,
  type JsonSchema,
  leafPaths,
  presentLeaves,
  SchemaInput,
  SECTION_LABELS,
  setAt,
  unsetAt,
} from "@/components/schema-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { fmtTime, isPlainObject } from "@/lib/format";
import { cn } from "@/lib/utils";

type SelectedScope = {
  scope: "guild" | "category" | "channel";
  id: string;
  guildId: string;
  label: string;
};

/**
 * Single-page collapsible 3-level KOOK tree (Guild -> Category -> Channel) + shared right-side sparse override editor.
 * Keeps 4-level inheritance (Global -> Guild -> Category -> Channel) compact and non-bloated.
 */
export function ChannelsPage() {
  const tree = useQuery(getChannelTreeOptions());
  const sync = useMutation({
    ...syncKookStructureMutation(),
    onSuccess: (r) =>
      toast.success(
        `已同步 ${r.guilds} 个服务器、${r.categories} 个分组、${r.channels} 个文字频道` +
          (r.forums ? `、${r.forums} 个帖子频道` : ""),
      ),
  });
  const upsertGuild = useMutation(upsertGuildMutation());
  const removeGuild = useMutation({ ...deleteGuildMutation(), onSuccess: () => toast.success("已删除服务器配置") });
  const upsertChannel = useMutation(upsertChannelMutation());
  const removeChannel = useMutation({ ...deleteChannelMutation(), onSuccess: () => toast.success("已删除频道配置") });
  const backfill = useMutation({ ...backfillChannelMutation(), onSuccess: () => toast.success("已请求回填历史消息") });
  const syncForum = useMutation({
    ...syncForumChannelThreadsMutation(),
    onSuccess: (r) => toast.success(`已同步 ${r.synced} 条帖子索引`),
  });

  const [selected, setSelected] = useState<SelectedScope | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const toggleCollapse = (key: string) => setCollapsed((c) => ({ ...c, [key]: !c[key] }));

  const saveGuild = (g: GuildOut, enabled: boolean) =>
    upsertGuild.mutate({
      path: { guild_id: g.guild_id },
      body: { name: g.name, icon: g.icon, enabled },
    });

  const saveChannel = (ch: ChannelOut, enabled: boolean | null) =>
    upsertChannel.mutate({
      path: { channel_id: ch.channel_id },
      body: {
        guild_id: ch.guild_id,
        parent_id: ch.parent_id,
        name: ch.name,
        topic: ch.topic,
        position: ch.position,
        is_category: ch.is_category,
        channel_type: ch.channel_type,
        enabled,
      },
    });

  const isSelected = (scope: SelectedScope["scope"], id: string) => selected?.scope === scope && selected.id === id;
  const selectNode = (next: SelectedScope) =>
    setSelected((cur) => (cur?.scope === next.scope && cur.id === next.id ? null : next));

  return (
    <div>
      <PageHeader
        title="服务器与频道"
        description="按「全局 → 服务器 → 分组 → 频道」四级稀疏继承控制功能开关、记忆共享、回复风格与触发词。点击右侧「覆盖」仅修改与上级不同的字段。"
        actions={
          <PendingButton pending={sync.isPending} onClick={() => sync.mutate({})}>
            {!sync.isPending && <RefreshCwIcon data-icon="inline-start" />}
            从 KOOK 同步
          </PendingButton>
        }
      />

      <QueryState isLoading={tree.isLoading} error={tree.error} />
      {tree.data?.length === 0 && (
        <Empty
          description="请先在「设置」页配置 KOOK Bot Token 并连接，然后点击右上角「从 KOOK 同步」拉取服务器、分组与频道。"
        >
          还没有同步任何服务器
        </Empty>
      )}

      {!!tree.data?.length && (
        <div className={cn("grid items-start gap-6", selected && "xl:grid-cols-[minmax(0,1fr)_32rem]")}>
          <div className="grid gap-4">
            {tree.data.map(({ guild, categories, ungrouped }) => {
              const gCollapsed = !!collapsed[`g:${guild.guild_id}`];
              const gOverrides = Object.keys(guild.settings ?? {});
              return (
                <Card key={guild.guild_id} className={cn(!guild.enabled && "opacity-70")}>
                  <CardHeader className="py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <Button
                        size="icon-xs"
                        variant="ghost"
                        aria-label={gCollapsed ? "展开服务器" : "折叠服务器"}
                        onClick={() => toggleCollapse(`g:${guild.guild_id}`)}
                      >
                        {gCollapsed ? <ChevronRightIcon /> : <ChevronDownIcon />}
                      </Button>
                      <ServerIcon className="size-4 text-muted-foreground" aria-hidden />
                      <CardTitle className="text-base">{guild.name || guild.guild_id}</CardTitle>
                      <span className="font-mono text-xs text-muted-foreground">{guild.guild_id}</span>
                      {gOverrides.length > 0 && (
                        <Badge variant="secondary" className="text-xs font-normal">
                          覆盖 {gOverrides.length} 节
                        </Badge>
                      )}
                      <div className="ml-auto flex items-center gap-2">
                        <Switch
                          aria-label={`启用服务器 ${guild.name || guild.guild_id}`}
                          checked={guild.enabled}
                          onCheckedChange={(en) => saveGuild(guild, en)}
                        />
                        <Button
                          size="xs"
                          variant={isSelected("guild", guild.guild_id) ? "default" : "outline"}
                          onClick={() =>
                            selectNode({
                              scope: "guild",
                              id: guild.guild_id,
                              guildId: guild.guild_id,
                              label: `服务器 ${guild.name || guild.guild_id}`,
                            })
                          }
                        >
                          <SlidersHorizontalIcon data-icon="inline-start" /> 覆盖
                        </Button>
                        <ConfirmButton
                          size="xs"
                          variant="ghost"
                          title={`删除服务器 ${guild.name || guild.guild_id} 的配置？`}
                          description="仅删除配置，不删除历史消息与记忆；重新同步可恢复。"
                          onConfirm={() => removeGuild.mutate({ path: { guild_id: guild.guild_id } })}
                        >
                          删除
                        </ConfirmButton>
                      </div>
                    </div>
                  </CardHeader>

                  {!gCollapsed && (
                    <CardContent className="grid gap-3 pt-0">
                      {categories.map(({ category, channels }) => {
                        const cCollapsed = !!collapsed[`c:${category.channel_id}`];
                        const cOverrides = Object.keys(category.settings ?? {});
                        return (
                          <div key={category.channel_id} className="rounded-lg border bg-muted/20">
                            <div className="flex flex-wrap items-center gap-2 px-3 py-2">
                              <Button
                                size="icon-xs"
                                variant="ghost"
                                aria-label={cCollapsed ? "展开分组" : "折叠分组"}
                                onClick={() => toggleCollapse(`c:${category.channel_id}`)}
                              >
                                {cCollapsed ? <ChevronRightIcon /> : <ChevronDownIcon />}
                              </Button>
                              <FolderIcon className="size-4 text-muted-foreground" aria-hidden />
                              <span className="text-sm font-medium">{category.name || category.channel_id}</span>
                              <span className="font-mono text-xs text-muted-foreground">{category.channel_id}</span>
                              <Badge variant="outline" className="text-xs font-normal">
                                {channels.length} 频道
                              </Badge>
                              {cOverrides.length > 0 && (
                                <Badge variant="secondary" className="text-xs font-normal">
                                  覆盖 {cOverrides.length} 节
                                </Badge>
                              )}
                              <div className="ml-auto flex items-center gap-2">
                                <Switch
                                  aria-label={`启用分组 ${category.name || category.channel_id}`}
                                  checked={Boolean(category.enabled)}
                                  onCheckedChange={(en) => saveChannel(category, en)}
                                />
                                <Button
                                  size="xs"
                                  variant={isSelected("category", category.channel_id) ? "default" : "outline"}
                                  onClick={() =>
                                    selectNode({
                                      scope: "category",
                                      id: category.channel_id,
                                      guildId: guild.guild_id,
                                      label: `分组 ${category.name || category.channel_id}`,
                                    })
                                  }
                                >
                                  <SlidersHorizontalIcon data-icon="inline-start" /> 覆盖
                                </Button>
                              </div>
                            </div>
                            {!cCollapsed && channels.length > 0 && (
                              <div className="divide-y border-t bg-background/80">
                                {channels.map((ch) => (
                                  <ChannelRow
                                    key={ch.channel_id}
                                    channel={ch}
                                    hasParent
                                    selected={isSelected("channel", ch.channel_id)}
                                    onSelect={() =>
                                      selectNode({
                                        scope: "channel",
                                        id: ch.channel_id,
                                        guildId: guild.guild_id,
                                        label: `频道 #${ch.name || ch.channel_id}`,
                                      })
                                    }
                                    onStateChange={(en) => saveChannel(ch, en)}
                                    onBackfill={() =>
                                      ch.channel_type === 4
                                        ? syncForum.mutate({ path: { channel_id: ch.channel_id } })
                                        : backfill.mutate({ path: { channel_id: ch.channel_id } })
                                    }
                                    backfilling={
                                      ch.channel_type === 4
                                        ? syncForum.isPending && syncForum.variables?.path.channel_id === ch.channel_id
                                        : backfill.isPending && backfill.variables?.path.channel_id === ch.channel_id
                                    }
                                    onDelete={() => removeChannel.mutate({ path: { channel_id: ch.channel_id } })}
                                  />
                                ))}
                              </div>
                            )}
                          </div>
                        );
                      })}

                      {ungrouped.length > 0 && (
                        <div className="rounded-lg border">
                          <div className="border-b bg-muted/20 px-3 py-1.5 text-xs font-medium text-muted-foreground">
                            未分组频道
                          </div>
                          <div className="divide-y">
                            {ungrouped.map((ch) => (
                              <ChannelRow
                                key={ch.channel_id}
                                channel={ch}
                                hasParent={false}
                                selected={isSelected("channel", ch.channel_id)}
                                onSelect={() =>
                                  selectNode({
                                    scope: "channel",
                                    id: ch.channel_id,
                                    guildId: guild.guild_id,
                                    label: `${ch.channel_type === 4 ? "帖子频道" : "频道"} #${ch.name || ch.channel_id}`,
                                  })
                                }
                                onStateChange={(en) => saveChannel(ch, en)}
                                onBackfill={() =>
                                  ch.channel_type === 4
                                    ? syncForum.mutate({ path: { channel_id: ch.channel_id } })
                                    : backfill.mutate({ path: { channel_id: ch.channel_id } })
                                }
                                backfilling={
                                  ch.channel_type === 4
                                    ? syncForum.isPending && syncForum.variables?.path.channel_id === ch.channel_id
                                    : backfill.isPending && backfill.variables?.path.channel_id === ch.channel_id
                                }
                                onDelete={() => removeChannel.mutate({ path: { channel_id: ch.channel_id } })}
                              />
                            ))}
                          </div>
                        </div>
                      )}
                    </CardContent>
                  )}
                </Card>
              );
            })}
          </div>

          {selected && (
            <div className="xl:sticky xl:top-6">
              <ScopeOverrideEditor
                key={`${selected.scope}:${selected.id}`}
                selected={selected}
                guildTree={tree.data.find((g) => g.guild.guild_id === selected.guildId)}
                onClose={() => setSelected(null)}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChannelRow({
  channel: ch,
  hasParent,
  selected,
  onSelect,
  onStateChange,
  onBackfill,
  backfilling,
  onDelete,
}: {
  channel: ChannelOut;
  hasParent: boolean;
  selected: boolean;
  onSelect: () => void;
  onStateChange: (enabled: boolean | null) => void;
  onBackfill: () => void;
  backfilling: boolean;
  onDelete: () => void;
}) {
  const overrides = Object.keys(ch.settings ?? {});
  const modeValue = ch.enabled === null ? "inherit" : ch.enabled ? "on" : "off";
  const isForum = ch.channel_type === 4;

  return (
    <div
      className={cn(
        "flex flex-wrap items-center gap-2 px-3 py-2 text-sm transition-colors",
        selected && "bg-primary/5",
        !ch.effective_enabled && "opacity-70",
      )}
    >
      {isForum ? (
        <MessagesSquareIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      ) : (
        <HashIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
      )}
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="font-medium">{ch.name || ch.channel_id}</span>
          <span className="font-mono text-xs text-muted-foreground">{ch.channel_id}</span>
          {isForum && (
            <Badge variant="secondary" className="text-[11px] font-normal">
              帖子频道
            </Badge>
          )}
          <Badge variant={ch.effective_enabled ? "default" : "outline"} className="text-[11px] font-normal">
            {ch.effective_enabled ? (isForum ? "可读" : "生效中") : "已停用"}
          </Badge>
          {overrides.length > 0 && (
            <Badge variant="secondary" className="text-[11px] font-normal">
              覆盖：{overrides.map((k) => SECTION_LABELS[k] ?? k).join("、")}
            </Badge>
          )}
        </div>
        {ch.backfilled_at && (
          <div className="text-xs text-muted-foreground">
            {isForum ? "上次同步帖子：" : "上次回填："}
            {fmtTime(ch.backfilled_at)}
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {hasParent ? (
          <OptionSelect
            aria-label={`频道 ${ch.name || ch.channel_id} 开关模式`}
            className="h-7 w-28 text-xs"
            value={modeValue}
            onChange={(v) => onStateChange(v === "inherit" ? null : v === "on")}
            options={[
              { value: "inherit", label: "继承分组" },
              { value: "on", label: "强制启用" },
              { value: "off", label: "强制停用" },
            ]}
          />
        ) : (
          <Switch
            aria-label={`启用频道 ${ch.name || ch.channel_id}`}
            checked={Boolean(ch.enabled)}
            onCheckedChange={(en) => onStateChange(en)}
          />
        )}
        <Button size="xs" variant={selected ? "default" : "outline"} onClick={onSelect}>
          <SlidersHorizontalIcon data-icon="inline-start" /> 覆盖
        </Button>
        <PendingButton size="xs" variant="outline" pending={backfilling} onClick={onBackfill}>
          {!backfilling &&
            (isForum ? <RefreshCwIcon data-icon="inline-start" /> : <DownloadIcon data-icon="inline-start" />)}
          {isForum ? "同步帖子" : "回填"}
        </PendingButton>
        <ConfirmButton
          size="xs"
          variant="ghost"
          title={`删除频道 #${ch.name || ch.channel_id} 的配置？`}
          description="只删除配置（开关和设置覆盖），不删除历史消息和记忆。"
          onConfirm={onDelete}
        >
          删除
        </ConfirmButton>
      </div>
    </div>
  );
}

const pathKey = (path: string[]) => path.join(".");

const QUICK_OVERRIDES: { path: string[]; label: string }[] = [
  { path: ["access", "allow_bot"], label: "是否可用 bot" },
  { path: ["access", "allowed_roles"], label: "角色白名单" },
  { path: ["access", "denied_roles"], label: "角色黑名单" },
  { path: ["access", "role_rules"], label: "按角色细分权限" },
  { path: ["ops", "role_rules"], label: "管理助手角色授权" },
  { path: ["tools", "enabled"], label: "可用工具" },
  { path: ["memory", "share_in_guild"], label: "对外共享记忆" },
  { path: ["memory", "cross_channel_read"], label: "跨频道读取" },
  { path: ["memory", "readable_by_scopes"], label: "定向放行来源" },
  { path: ["memory", "readable_by_roles"], label: "定向放行角色" },
  { path: ["memory", "cross_channel_allow"], label: "引用白名单" },
  { path: ["memory", "cross_channel_deny"], label: "禁止引用黑名单" },
];

type MemoryScopeListField = "readable_by_scopes" | "cross_channel_allow" | "cross_channel_deny";

/** Shared sparse-diff override editor for Guild / Category / Channel nodes. */
function ScopeOverrideEditor({
  selected,
  guildTree,
  onClose,
}: {
  selected: SelectedScope;
  guildTree?: GuildTreeOut;
  onClose: () => void;
}) {
  const schemaQ = useQuery(getSettingsSchemaOptions());
  const settingsQ = useQuery(
    getScopeSettingsOptions({ path: { scope: selected.scope, scope_id: selected.id } }),
  );
  const rolesQ = useQuery({
    ...listKookGuildRolesOptions({ path: { guild_id: selected.guildId } }),
    retry: false,
  });
  const save = useMutation({
    ...putScopeSettingsMutation(),
    onSuccess: () => toast.success(`已保存「${selected.label}」的覆盖设置`),
  });
  const [doc, setDoc] = useState<Record<string, unknown>>({});
  const [picking, setPicking] = useState(false);
  const [rolesOpen, setRolesOpen] = useState(false);
  const [scopesOpen, setScopesOpen] = useState(false);

  useEffect(() => {
    if (settingsQ.data) setDoc(settingsQ.data.value);
  }, [settingsQ.data]);

  const root = schemaQ.data as JsonSchema | undefined;
  if (!root || !settingsQ.data) {
    return (
      <QueryState
        isLoading={schemaQ.isLoading || settingsQ.isLoading}
        error={schemaQ.error ?? settingsQ.error}
        variant="card"
      />
    );
  }

  const inherited = settingsQ.data.defaults;
  const present = presentLeaves(doc, root, root).filter((l) => l.path.length);
  const presentKeys = new Set(present.map((l) => pathKey(l.path)));
  const available = leafPaths(root, root).filter((l) => !presentKeys.has(pathKey(l.path)));
  const sections = [...new Set(available.map((l) => l.path[0]))];
  const dirty = JSON.stringify(doc) !== JSON.stringify(settingsQ.data.value);

  const ensureOverride = (path: string[]) => {
    if (!presentKeys.has(pathKey(path))) {
      const initial = pathKey(path) === "access.role_rules" ? {} : getAt(inherited, path);
      setDoc((cur) => setAt(cur, path, initial));
    }
  };

  const toggleStringList = (path: string[], itemId: string) => {
    const current = getAt(doc, path) ?? getAt(inherited, path);
    const list = Array.isArray(current) ? current.map(String) : [];
    const next = list.includes(itemId) ? list.filter((r) => r !== itemId) : [...list, itemId];
    setDoc((cur) => setAt(cur, path, next));
  };

  const toggleRoleList = (field: "allowed_roles" | "denied_roles", roleId: string) =>
    toggleStringList(["access", field], roleId);

  const toggleMemoryScopeList = (field: MemoryScopeListField, scopeId: string) =>
    toggleStringList(["memory", field], scopeId);

  const roleRulePath = (roleId: string) => ["access", "role_rules", roleId];
  type RolePermission = "bypass_channel_privacy" | "azuriom_lookup_others";

  const addRoleRule = (roleId: string) => {
    const path = roleRulePath(roleId);
    setDoc((cur) => (getAt(cur, path) === undefined ? setAt(cur, path, {}) : cur));
  };

  const effectiveRolePermission = (value: Record<string, unknown>, path: string[]) =>
    (getAt(value, path) ?? getAt(inherited, path)) === true;

  const toggleRoleRule = (roleId: string, field: RolePermission) => {
    const path = [...roleRulePath(roleId), field];
    setDoc((cur) => setAt(cur, path, !effectiveRolePermission(cur, path)));
  };

  const toggleBypassPrivacy = (roleId: string) => toggleRoleRule(roleId, "bypass_channel_privacy");

  const toggleAzuriomLookupOthers = (roleId: string) => toggleRoleRule(roleId, "azuriom_lookup_others");

  const effStringSet = (path: string[]) => {
    const raw = getAt(doc, path) ?? getAt(inherited, path);
    return new Set((Array.isArray(raw) ? (raw as unknown[]) : []).map(String));
  };

  const effAllowed = effStringSet(["access", "allowed_roles"]);
  const effDenied = effStringSet(["access", "denied_roles"]);
  const effReadableRoles = effStringSet(["memory", "readable_by_roles"]);
  const effReadableScopes = effStringSet(["memory", "readable_by_scopes"]);
  const effCrossAllow = effStringSet(["memory", "cross_channel_allow"]);
  const effCrossDeny = effStringSet(["memory", "cross_channel_deny"]);
  const roleRuleIds = new Set(
    [inherited, doc].flatMap((value) => {
      const rules = getAt(value, ["access", "role_rules"]);
      return isPlainObject(rules) ? Object.keys(rules) : [];
    }),
  );

  const scopeItems: { id: string; label: string; isCategory: boolean }[] = [];
  if (guildTree) {
    for (const { category, channels } of guildTree.categories) {
      scopeItems.push({ id: category.channel_id, label: `分组 ${category.name || category.channel_id}`, isCategory: true });
      for (const ch of channels) {
        scopeItems.push({ id: ch.channel_id, label: `#${ch.name || ch.channel_id}`, isCategory: false });
      }
    }
    for (const ch of guildTree.ungrouped) {
      scopeItems.push({ id: ch.channel_id, label: `#${ch.name || ch.channel_id}`, isCategory: false });
    }
  }

  const activeRoleHits =
    effAllowed.size +
    effDenied.size +
    effReadableRoles.size +
    roleRuleIds.size;
  const activeScopeHits = effReadableScopes.size + effCrossAllow.size + effCrossDeny.size;

  return (
    <Card className="flex flex-col xl:max-h-[calc(100vh-3rem)]">
      <CardHeader className="shrink-0 border-b pb-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <CardTitle className="truncate text-base">{selected.label}</CardTitle>
            <CardDescription className="mt-1 truncate text-xs" title={(settingsQ.data.chain ?? []).join(" → ")}>
              继承链：{(settingsQ.data.chain ?? []).join(" → ")}
            </CardDescription>
          </div>
          <Button size="icon-xs" variant="ghost" aria-label="关闭覆盖面板" onClick={onClose}>
            <XIcon />
          </Button>
        </div>
      </CardHeader>
      <CardContent className="min-h-0 flex-1 overflow-y-auto">
        <Tabs defaultValue="form">
          <TabsList>
            <TabsTrigger value="form">表单</TabsTrigger>
            <TabsTrigger value="json">JSON</TabsTrigger>
          </TabsList>
          <TabsContent value="form" className="grid gap-3 pt-2">
            <div className="flex flex-wrap gap-1.5">
              {QUICK_OVERRIDES.filter((q) => !presentKeys.has(pathKey(q.path))).map((q) => (
                <Button key={pathKey(q.path)} size="xs" variant="outline" onClick={() => ensureOverride(q.path)}>
                  <PlusIcon data-icon="inline-start" /> {q.label}
                </Button>
              ))}
            </div>
            {!!rolesQ.data?.length && (
              <div className="rounded-lg border bg-muted/20">
                <button
                  type="button"
                  onClick={() => setRolesOpen((o) => !o)}
                  className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
                >
                  {rolesOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
                  <span className="flex-1">快捷设置：本服 KOOK 角色（{rolesQ.data.length}）</span>
                  {activeRoleHits > 0 && <Badge variant="secondary">已配 {activeRoleHits}</Badge>}
                </button>
                {rolesOpen && (
                  <div className="grid max-h-48 gap-1.5 overflow-y-auto border-t px-2.5 py-2">
                    {rolesQ.data.map((r) => {
                      const inAllow = effAllowed.has(r.role_id);
                      const inDeny = effDenied.has(r.role_id);
                      const inReadable = effReadableRoles.has(r.role_id);
                      const path = roleRulePath(r.role_id);
                      const bypass = effectiveRolePermission(doc, [...path, "bypass_channel_privacy"]);
                      const lookupOthers = effectiveRolePermission(doc, [...path, "azuriom_lookup_others"]);
                      return (
                        <div key={r.role_id} className="flex flex-wrap items-center gap-1 text-xs">
                          <span className="min-w-24 flex-1 truncate font-medium" title={`${r.name} (${r.role_id})`}>
                            {r.name}
                          </span>
                          <Button
                            size="xs"
                            variant={inAllow ? "default" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            onClick={() => toggleRoleList("allowed_roles", r.role_id)}
                          >
                            {inAllow ? "已白名单" : "+白名单"}
                          </Button>
                          <Button
                            size="xs"
                            variant={inDeny ? "destructive" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            onClick={() => toggleRoleList("denied_roles", r.role_id)}
                          >
                            {inDeny ? "已禁用" : "+禁用"}
                          </Button>
                          <Button
                            size="xs"
                            variant={inReadable ? "secondary" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            title="即使本频道/分组关闭对外共享，也允许该角色读取"
                            onClick={() => toggleStringList(["memory", "readable_by_roles"], r.role_id)}
                          >
                            {inReadable ? "可读私密" : "+放行可读"}
                          </Button>
                          <Button
                            size="xs"
                            variant={bypass ? "default" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            title="允许该角色在当前范围内绕过跨频道隐私限制"
                            onClick={() => toggleBypassPrivacy(r.role_id)}
                          >
                            {bypass ? "已绕过隐私" : "+绕过隐私"}
                          </Button>
                          <Button
                            size="xs"
                            variant={lookupOthers ? "default" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            title="允许该角色在普通对话中查他人绑定或按 SteamID 反查 KOOK"
                            onClick={() => toggleAzuriomLookupOthers(r.role_id)}
                          >
                            {lookupOthers ? "可查他人绑定" : "+查他人绑定"}
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            className="h-6 px-1.5 text-[11px]"
                            onClick={() => addRoleRule(r.role_id)}
                          >
                            +工具
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
            {scopeItems.length > 0 && (
              <div className="rounded-lg border bg-muted/20">
                <button
                  type="button"
                  onClick={() => setScopesOpen((o) => !o)}
                  className="flex w-full items-center gap-1.5 px-2.5 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
                >
                  {scopesOpen ? <ChevronDownIcon className="size-3.5" /> : <ChevronRightIcon className="size-3.5" />}
                  <span className="flex-1">快捷设置：本服分组与频道（{scopeItems.length}）</span>
                  {activeScopeHits > 0 && <Badge variant="secondary">已配 {activeScopeHits}</Badge>}
                </button>
                {scopesOpen && (
                  <div className="grid max-h-48 gap-1.5 overflow-y-auto border-t px-2.5 py-2">
                    {scopeItems.map((item) => {
                      const inReadable = effReadableScopes.has(item.id);
                      const inCrossAllow = effCrossAllow.has(item.id);
                      const inCrossDeny = effCrossDeny.has(item.id);
                      return (
                        <div key={item.id} className="flex flex-wrap items-center gap-1 text-xs">
                          <span
                            className={cn(
                              "min-w-24 flex-1 truncate",
                              item.isCategory ? "font-medium" : "pl-2 text-muted-foreground",
                            )}
                            title={`${item.label} (${item.id})`}
                          >
                            {item.label}
                          </span>
                          <Button
                            size="xs"
                            variant={inReadable ? "default" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            title="允许该来源频道/分组读取本隐私频道（memory.readable_by_scopes）"
                            onClick={() => toggleMemoryScopeList("readable_by_scopes", item.id)}
                          >
                            {inReadable ? "已放行来源" : "+放行来源"}
                          </Button>
                          <Button
                            size="xs"
                            variant={inCrossAllow ? "secondary" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            title="在当前频道允许引用的目标白名单（memory.cross_channel_allow）"
                            onClick={() => toggleMemoryScopeList("cross_channel_allow", item.id)}
                          >
                            {inCrossAllow ? "引用白名单" : "+引用白"}
                          </Button>
                          <Button
                            size="xs"
                            variant={inCrossDeny ? "destructive" : "outline"}
                            className="h-6 px-1.5 text-[11px]"
                            title="在当前频道禁止引用的目标黑名单（memory.cross_channel_deny）"
                            onClick={() => toggleMemoryScopeList("cross_channel_deny", item.id)}
                          >
                            {inCrossDeny ? "已禁引用" : "+禁引用"}
                          </Button>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}
            {present.length === 0 && (
              <p className="rounded-lg border border-dashed p-4 text-center text-xs text-muted-foreground">
                当前未覆盖任何字段，全部沿用上级配置。
              </p>
            )}
            {present.map(({ path, schema }) => (
              <div key={pathKey(path)} className="flex min-w-0 items-start gap-2 rounded-lg border p-3">
                <Field
                  className="min-w-0 flex-1"
                  label={
                    <span className="inline-flex flex-wrap items-baseline gap-x-2">
                      <span className="text-muted-foreground">{SECTION_LABELS[path[0]] ?? path[0]}</span>
                      <span className="font-mono text-xs">{path.slice(1).join(".") || path[0]}</span>
                    </span>
                  }
                  hint={
                    <>
                      {schema.description && <span className="block">{schema.description}</span>}
                      <span className="block break-all">上级继承值：{JSON.stringify(getAt(inherited, path))}</span>
                    </>
                  }
                >
                  <SchemaInput
                    schema={schema}
                    root={root}
                    keyOptions={pathKey(path) === "ops.role_rules" ? rolesQ.data?.map((r) => ({
                      value: r.role_id, label: `${r.name}（${r.role_id}）`,
                    })) : undefined}
                    value={getAt(doc, path)}
                    onChange={(v) => setDoc(setAt(doc, path, v))}
                  />
                </Field>
                <Button
                  size="icon-xs"
                  variant="ghost"
                  aria-label={`移除覆盖 ${pathKey(path)}`}
                  onClick={() => setDoc(unsetAt(doc, path))}
                >
                  <Trash2Icon />
                </Button>
              </div>
            ))}
            <Popover open={picking} onOpenChange={setPicking}>
              <PopoverTrigger asChild>
                <Button
                  size="sm"
                  variant="outline"
                  role="combobox"
                  aria-expanded={picking}
                  disabled={!available.length}
                  className="w-fit"
                >
                  <PlusIcon data-icon="inline-start" /> 添加覆盖项
                  <ChevronsUpDownIcon data-icon="inline-end" className="text-muted-foreground" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-96 p-0" align="start">
                <Command>
                  <CommandInput aria-label="搜索设置项" placeholder="搜索功能开关、记忆共享、回复风格、别名…" />
                  <CommandList>
                    <CommandEmpty>没有匹配的设置项</CommandEmpty>
                    {sections.map((section) => (
                      <CommandGroup key={section} heading={SECTION_LABELS[section] ?? section}>
                        {available
                          .filter((l) => l.path[0] === section)
                          .map((l) => (
                            <CommandItem
                              key={pathKey(l.path)}
                              value={pathKey(l.path)}
                              keywords={[SECTION_LABELS[section] ?? section, l.schema.description ?? ""]}
                              onSelect={() => {
                                ensureOverride(l.path);
                                setPicking(false);
                              }}
                            >
                              <span className="font-mono text-xs">{l.path.slice(1).join(".") || section}</span>
                            </CommandItem>
                          ))}
                      </CommandGroup>
                    ))}
                  </CommandList>
                </Command>
              </PopoverContent>
            </Popover>
          </TabsContent>
          <TabsContent value="json" className="pt-2">
            <Field label="覆盖文档（JSON）">
              <JsonEditor value={doc} rows={12} onChange={(v) => isPlainObject(v) && setDoc(v)} />
            </Field>
          </TabsContent>
        </Tabs>
      </CardContent>
      <CardFooter className="shrink-0 gap-2">
        <PendingButton
          size="sm"
          pending={save.isPending}
          disabled={!dirty}
          onClick={() =>
            save.mutate({ path: { scope: selected.scope, scope_id: selected.id }, body: doc })
          }
        >
          保存覆盖
        </PendingButton>
        <Button size="sm" variant="outline" disabled={!dirty} onClick={() => setDoc(settingsQ.data.value)}>
          还原
        </Button>
      </CardFooter>
    </Card>
  );
}
