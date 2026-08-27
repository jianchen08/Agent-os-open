/**
 * 插件管理设置页面（VSCode 扩展面板式）
 *
 * 对齐安装触发模型（docs/working/重要设计/插件安装与触发模型设计.md §七），
 * 布局对齐 VSCode Extensions 视图（卡片结构为插件市场预留：图标方块 +
 * 名称/版本 + 描述 + 右侧动作位）：
 * - 顶部统一搜索框（sticky）：同时过滤插件（name/id/描述/类型）与工具能力
 *   （工具名/描述/所属插件）——单一搜索入口，不分区各自设框
 * - 视图分段：全部 / System / Pipeline / Tool / 已禁用
 * - 插件卡片：类型图标 + Enabled 开关（PUT /api/v1/plugins/{id}/enabled）；
 *   描述来自 manifest 透传（内核 plugins_status_handler）
 * - 工具能力浏览（ToolsPage 退役后并入：/api/v1/schema 聚合的 tools 面，
 *   受同一搜索词过滤 + 展开 input_schema 摘要）
 *
 * 恒为嵌入形态（SettingsHubWidget / PanelHostWidget 两个消费方均内嵌渲染）。
 */

import { useQuery } from '@tanstack/react-query'
import { useState, useEffect } from 'react'
import {
  RefreshCw,
  AlertCircle,
  Plug,
  ToggleLeft,
  Wrench,
  Search,
  Settings,
  Zap,
  X,
  type LucideIcon,
} from '@/assets/icons'
import { PageShell } from '@/components/shared/PageShell'
import { toast } from '@/components/ui/sonner'
import apiClient from '@/services/api/client'
import { refreshPluginContributions } from '@/services/modules/GrowthLoop'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'

/** 插件状态信息（对齐后端 plugins_status_handler 返回） */
interface PluginStatus {
  plugin_id: string
  name: string
  description?: string | null
  config_type: string
  host_type: string
  version: string | null
  enabled: boolean
  activation: string
  status: string
  config_files: Array<{ id: string; label: string; path: string }>
  has_contributes: boolean
  has_http_endpoints: boolean
  error: string | null
}

/** 工具能力条目（/api/v1/schema 的 tools 面，ToolDescriptor 序列化子集） */
interface ToolCapability {
  name: string
  description?: string
  plugin_id?: string
  category?: string
  source?: string
  input_schema?: Record<string, unknown>
}

/** 类型主题：卡片徽标 + 图标方块共用一套配色。
 *  半透明底/边框用 color-mix 绑定 ds 语义令牌（同 ActivityCard 模式），
 *  跟随主题切换；fallback 与旧硬编码 hex 一致，令牌缺席时渲染不变。 */
function typeTheme(configType: string): {
  label: string
  badgeClass: string
  boxClass: string
  Icon: LucideIcon
} {
  const t = (configType || '').toLowerCase()
  if (t.includes('pipeline')) {
    return {
      label: 'Pipeline',
      badgeClass:
        'bg-[color-mix(in_srgb,var(--ds-accent-ai,#A78BFA)_15%,transparent)] text-[var(--ds-accent-ai,#A78BFA)] border-[color-mix(in_srgb,var(--ds-accent-ai,#A78BFA)_35%,transparent)]',
      boxClass:
        'bg-[color-mix(in_srgb,var(--ds-accent-ai,#A78BFA)_12%,transparent)] border-[color-mix(in_srgb,var(--ds-accent-ai,#A78BFA)_35%,transparent)] text-[var(--ds-accent-ai,#A78BFA)]',
      Icon: Zap,
    }
  }
  if (t.includes('tool')) {
    return {
      label: 'Tool',
      badgeClass:
        'bg-[color-mix(in_srgb,var(--ds-accent-primary,#22D3EE)_12%,transparent)] text-[var(--ds-accent-primary,#22D3EE)] border-[color-mix(in_srgb,var(--ds-accent-primary,#22D3EE)_35%,transparent)]',
      boxClass:
        'bg-[color-mix(in_srgb,var(--ds-accent-primary,#22D3EE)_10%,transparent)] border-[color-mix(in_srgb,var(--ds-accent-primary,#22D3EE)_35%,transparent)] text-[var(--ds-accent-primary,#22D3EE)]',
      Icon: Wrench,
    }
  }
  if (t.includes('system')) {
    return {
      label: 'System',
      badgeClass:
        'bg-[color-mix(in_srgb,var(--ds-status-info,#60A5FA)_12%,transparent)] text-[var(--ds-status-info,#60A5FA)] border-[color-mix(in_srgb,var(--ds-status-info,#60A5FA)_35%,transparent)]',
      boxClass:
        'bg-[color-mix(in_srgb,var(--ds-status-info,#60A5FA)_10%,transparent)] border-[color-mix(in_srgb,var(--ds-status-info,#60A5FA)_35%,transparent)] text-[var(--ds-status-info,#60A5FA)]',
      Icon: Settings,
    }
  }
  return {
    label: configType || 'Composite',
    badgeClass: 'bg-[var(--hover-overlay)] text-muted-foreground border-border',
    boxClass: 'bg-[var(--hover-overlay)] border-border text-muted-foreground',
    Icon: Plug,
  }
}

/** activation 中文 */
function activationLabel(a: string): string {
  return { eager: '启动即载', lazy: '按需载入', manual: '手动启动' }[a] || a
}

export function PluginsSettingsPage() {
  const [plugins, setPlugins] = useState<PluginStatus[]>([])
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'pipeline' | 'tool' | 'system' | 'disabled'>('all')
  // 统一搜索词：同时作用于插件列表与工具能力区（单一搜索入口）
  const [search, setSearch] = useState('')
  const [expandedTool, setExpandedTool] = useState<string | null>(null)

  // 插件状态 + 工具能力面（query 化）：插件启停后由 toggle 乐观更新缓存，
  // 重进设置页缓存秒开
  const pluginsQuery = useQuery({
    queryKey: queryKeys.plugins,
    queryFn: async () => {
      const res = await apiClient.get<PluginStatus[]>('/api/v1/plugins')
      // 能力面与插件面同拉（读失败不阻断插件列表——能力区降级空）
      let tools: ToolCapability[] = []
      try {
        const schema = await apiClient.get<{ tools?: ToolCapability[] }>('/api/v1/schema')
        tools = Array.isArray(schema.data?.tools) ? schema.data.tools : []
      } catch {
        tools = []
      }
      return { plugins: res.data, capabilities: tools }
    },
    staleTime: 60_000,
  })
  const isLoading = pluginsQuery.isPending && plugins.length === 0
  const error = pluginsQuery.isError
    ? pluginsQuery.error instanceof Error ? pluginsQuery.error.message : '获取插件状态失败'
    : null

  useEffect(() => {
    if (pluginsQuery.data) {
      setPlugins(pluginsQuery.data.plugins)
    }
  }, [pluginsQuery.data])

  /** 切换插件启用状态 */
  const handleToggleEnabled = async (pluginId: string, currentEnabled: boolean) => {
    setTogglingId(pluginId)
    try {
      const res = await apiClient.put<{ success: boolean; message?: string; error?: string }>(
        `/api/v1/plugins/${pluginId}/enabled`,
        { enabled: !currentEnabled },
      )
      if (res.data.success) {
        // 缓存乐观更新（立即反映，重启后内核才真正生效）
        queryClient.setQueryData<{ plugins: PluginStatus[] }>(queryKeys.plugins, (prev) =>
          prev
            ? {
                ...prev,
                plugins: prev.plugins.map((p) =>
                  p.plugin_id === pluginId
                    ? { ...p, enabled: !currentEnabled, status: !currentEnabled ? 'active' : 'disabled' }
                    : p,
                ),
              }
            : prev,
        )
        setPlugins((prev) =>
          prev.map((p) =>
            p.plugin_id === pluginId
              ? { ...p, enabled: !currentEnabled, status: !currentEnabled ? 'active' : 'disabled' }
              : p,
          ),
        )
        toast.success(res.data.message || `已${!currentEnabled ? '启用' : '禁用'} ${pluginId}`)
        // 刷新插件贡献（contributes 仅 Enabled 插件导出）：
        // 禁用 → 其主题从列表移除（在用则回退 base）、注入 CSS 清理；
        // 启用 → 其主题/样式重新注入。失败不影响开关结果（仅 warn）。
        void refreshPluginContributions()
      } else {
        toast.error(res.data.error || '操作失败')
      }
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : '操作失败')
    } finally {
      setTogglingId(null)
    }
  }

  const q = search.trim().toLowerCase()
  const disabledCount = plugins.filter((p) => !p.enabled).length
  const filtered = plugins.filter((p) => {
    if (filter === 'disabled') {
      if (p.enabled) return false
    } else if (filter !== 'all' && !(p.config_type || '').toLowerCase().includes(filter)) {
      return false
    }
    if (!q) return true
    return (
      p.name?.toLowerCase().includes(q) ||
      p.plugin_id?.toLowerCase().includes(q) ||
      (p.description ?? '').toLowerCase().includes(q) ||
      (p.config_type || '').toLowerCase().includes(q)
    )
  })
  const capFiltered = pluginsQuery.data?.capabilities
    ? pluginsQuery.data.capabilities.filter((t) => {
        if (!q) return true
        return (
          t.name?.toLowerCase().includes(q) ||
          t.description?.toLowerCase().includes(q) ||
          t.plugin_id?.toLowerCase().includes(q)
        )
      })
    : []
  const capabilities = pluginsQuery.data?.capabilities ?? []

  const filterTabs: Array<{ id: typeof filter; label: string }> = [
    { id: 'all', label: `全部 ${plugins.length}` },
    { id: 'system', label: 'System' },
    { id: 'pipeline', label: 'Pipeline' },
    { id: 'tool', label: 'Tool' },
    { id: 'disabled', label: `已禁用 ${disabledCount}` },
  ]

  return (
    <PageShell title="插件管理" embedded mainLabel="插件管理面板">
      <div className="space-y-4">
        {/* 顶部统一搜索栏（sticky）：一个搜索框同时过滤插件与工具能力 */}
        <div
          className="sticky top-0 z-10 -mx-6 -mt-6 border-b px-6 pb-2.5 pt-4"
          style={{
            background: 'var(--ds-bg-panel, #0A1226)',
            borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
          }}
        >
          <div className="flex items-center gap-2">
            <div className="relative min-w-0 flex-1">
              <Search className="text-muted-foreground pointer-events-none absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2" />
              <input
                type="text"
                placeholder="搜索插件、工具…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                aria-label="搜索插件和工具"
                data-testid="plugins-search-input"
                className="bg-background focus:ring-primary w-full rounded-lg border py-1.5 pr-7 pl-8 text-xs focus:ring-1 focus:outline-none"
              />
              {search && (
                <button
                  onClick={() => setSearch('')}
                  aria-label="清空搜索"
                  className="text-muted-foreground hover:text-foreground absolute top-1/2 right-2 -translate-y-1/2"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <button
              onClick={() => void pluginsQuery.refetch()}
              disabled={isLoading}
              className="hover:bg-[var(--hover-overlay)] flex shrink-0 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
              style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
              刷新
            </button>
          </div>
          {/* 视图分段 + 启用计数 */}
          <div className="mt-2 flex flex-wrap items-center gap-1">
            {filterTabs.map((tab) => (
              <button
                key={tab.id}
                onClick={() => setFilter(tab.id)}
                className={`rounded-md px-2.5 py-1 text-[11px] transition-colors ${
                  filter === tab.id
                    ? 'text-[var(--ds-accent-primary,#22D3EE)]'
                    : 'text-muted-foreground hover:text-foreground'
                }`}
                style={
                  filter === tab.id
                    ? {
                        background: 'var(--ds-bg-elevated, #111C38)',
                        boxShadow: 'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))',
                      }
                    : undefined
                }
              >
                {tab.label}
              </button>
            ))}
            <span className="text-muted-foreground ml-auto font-mono text-[10px]" data-testid="plugins-enabled-count">
              {plugins.filter((p) => p.enabled).length}/{plugins.length} 启用
            </span>
          </div>
        </div>

        {error && <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>}

        {/* 加载骨架 */}
        {isLoading && (
          <div className="flex flex-col gap-2">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="animate-pulse rounded-lg border p-3.5">
                <div className="bg-muted mb-2 h-4 w-2/3 rounded" />
                <div className="bg-muted h-3 w-full rounded" />
              </div>
            ))}
          </div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && plugins.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16">
            <Plug className="text-muted-foreground/40 mb-3 h-12 w-12" />
            <p className="text-muted-foreground text-sm">暂无已注册的插件</p>
          </div>
        )}

        {/* 插件列表（VSCode 扩展卡片式） */}
        {!isLoading && !error && plugins.length > 0 && (
          <div className="flex flex-col gap-2" aria-live="polite">
            {filtered.length === 0 && (
              <p className="text-muted-foreground py-8 text-center text-sm">没有匹配的插件</p>
            )}
            {filtered.map((plugin) => {
              const theme = typeTheme(plugin.config_type)
              return (
                <div
                  key={plugin.plugin_id}
                  className="rounded-lg border p-3"
                  style={{
                    background: plugin.enabled
                      ? 'var(--ds-bg-panel, #0A1226)'
                      : 'color-mix(in srgb, var(--muted-foreground, #94a3b8) 4%, transparent)',
                    borderColor: plugin.error
                      ? 'color-mix(in srgb, var(--ds-status-error, #F87171) 55%, transparent)'
                      : 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
                    opacity: plugin.enabled ? 1 : 0.6,
                  }}
                >
                  <div className="flex items-start gap-3">
                    {/* 左：类型图标方块（市场化后可替换为 manifest 声明图标） */}
                    <div
                      className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-md border ${theme.boxClass}`}
                      aria-hidden="true"
                    >
                      <theme.Icon className="h-5 w-5" />
                    </div>
                    {/* 中：名称/版本 + 描述 + 元信息 */}
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                        <h3 className="text-foreground truncate text-[13px] font-medium" title={plugin.plugin_id}>
                          {plugin.name || plugin.plugin_id}
                        </h3>
                        {plugin.version && (
                          <span className="text-muted-foreground font-mono text-[10px]">v{plugin.version}</span>
                        )}
                        <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${theme.badgeClass}`}>
                          {theme.label}
                        </span>
                        {!plugin.enabled && (
                          <span className="text-muted-foreground flex items-center gap-1 text-[10px]">
                            <ToggleLeft className="h-3 w-3" />
                            已禁用（不贡献工具/路由/UI）
                          </span>
                        )}
                      </div>
                      <p className="text-muted-foreground mt-0.5 line-clamp-2 text-[11px]">
                        {plugin.description || plugin.plugin_id}
                      </p>
                      <div className="text-muted-foreground mt-1 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[10px]">
                        <span className="text-[var(--ds-accent-primary,#22D3EE)]">
                          {activationLabel(plugin.activation)}
                        </span>
                        <span>{plugin.host_type}</span>
                        {plugin.has_contributes && <span title="有 UI 贡献">🎨 界面贡献</span>}
                        {plugin.has_http_endpoints && <span title="有 HTTP 端点">🌐 端点</span>}
                        {plugin.config_files.length > 0 && (
                          <span>{plugin.config_files.length} 个配置文件（在左侧「插件配置」编辑）</span>
                        )}
                      </div>
                      {plugin.error && (
                        <div className="bg-status-error/10 text-status-error mt-1.5 flex items-start gap-1 rounded p-2 text-xs">
                          <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
                          <span className="line-clamp-2">{plugin.error}</span>
                        </div>
                      )}
                    </div>
                    {/* 右：启用开关 */}
                    <button
                      onClick={() => handleToggleEnabled(plugin.plugin_id, plugin.enabled)}
                      disabled={togglingId === plugin.plugin_id}
                      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 transition-colors duration-200 disabled:opacity-50 ${
                        plugin.enabled
                          ? 'bg-[var(--ds-accent-primary,#22D3EE)] border-[var(--ds-accent-primary,#22D3EE)]'
                          : 'bg-[var(--status-pending)] border-[var(--status-pending)]'
                      }`}
                      title={plugin.enabled ? '点击禁用（重启生效）' : '点击启用（重启生效）'}
                      aria-label={`${plugin.enabled ? '禁用' : '启用'} ${plugin.name || plugin.plugin_id}`}
                    >
                      <span
                        className={`inline-block h-3.5 w-3.5 transform rounded-full bg-primary-foreground transition duration-200 ${
                          plugin.enabled ? 'translate-x-3.5' : 'translate-x-0'
                        }`}
                      />
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}

        {/* ── 工具能力浏览（ToolsPage 退役并入；受顶部统一搜索词过滤）── */}
        {!isLoading && !error && capabilities.length > 0 && (
          <section className="mt-2" aria-label="工具能力浏览">
            <div className="mb-2 flex flex-wrap items-center gap-3">
              <h2 className="flex items-center gap-1.5 text-sm font-medium">
                <Wrench className="h-3.5 w-3.5" />
                工具能力
              </h2>
              <span className="text-muted-foreground font-mono text-[10px]">
                {q ? `${capFiltered.length}/${capabilities.length}` : capabilities.length} 个（LLM
                可见面，/api/v1/schema 聚合）
              </span>
            </div>
            <div className="flex flex-col gap-1.5">
              {capFiltered.length === 0 && (
                <p className="text-muted-foreground py-4 text-center text-xs">没有匹配的工具</p>
              )}
              {capFiltered.map((tool) => (
                <div
                  key={tool.name}
                  className="hover:bg-[var(--hover-overlay)] cursor-pointer rounded-lg border p-2.5"
                  style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
                  onClick={() => setExpandedTool(expandedTool === tool.name ? null : tool.name)}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px] font-medium" title={tool.name}>
                      {tool.name}
                    </span>
                    {tool.plugin_id && (
                      <span className="text-muted-foreground font-mono text-[10px]">@{tool.plugin_id}</span>
                    )}
                    {tool.category && (
                      <span className="rounded border-[color-mix(in_srgb,var(--ds-accent-primary,#22D3EE)_35%,transparent)] bg-[color-mix(in_srgb,var(--ds-accent-primary,#22D3EE)_12%,transparent)] border px-1.5 py-0.5 font-mono text-[10px]">
                        {tool.category}
                      </span>
                    )}
                    {tool.source && (
                      <span className="text-muted-foreground rounded bg-[var(--hover-overlay)] px-1.5 py-0.5 font-mono text-[10px]">
                        {tool.source}
                      </span>
                    )}
                  </div>
                  {tool.description && (
                    <p className="text-muted-foreground mt-1 line-clamp-2 text-[11px]">{tool.description}</p>
                  )}
                  {expandedTool === tool.name && tool.input_schema && (
                    <pre className="bg-muted/50 mt-2 max-h-48 overflow-auto rounded p-2 font-mono text-[10px] leading-relaxed">
                      {JSON.stringify(tool.input_schema, null, 2)}
                    </pre>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}
      </div>
    </PageShell>
  )
}
