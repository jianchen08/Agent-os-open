/**
 * 设置中枢 Widget · Deep Space v2
 *
 * 「设置」唯一 UI（无独立路由页 /settings，设置一律在工作区页签打开）：
 * - 左：设置导航树，三个分组按 内核设置 → 插件页面 → 插件配置 排列，
 *   分组以「图标 + 标题 + 计数 + 分隔线」的区块头区分区域
 * - 右：对应配置页内嵌区域（canvas 底色，与左导航 panel 底色区分）
 *
 * 深链：props.initialActive（如 `plugin:{pluginId}:{fileId}`）——外部入口
 * （管道编辑器 step 节点等）打开设置页签时直接定位到指定配置页。
 */

import { useEffect, useMemo, useState } from 'react'
import { FileCode, FileText, LayoutGrid, PluginIcon, Settings, type LucideIcon } from '@/assets/icons'
import { PluginConfigEditor } from '@/components/config/PluginConfigEditor'
import { renderPageContent } from '@/components/schema/PageRenderer'
import { cn } from '@/lib/utils'
import { PipelineSettingsPage } from '@/pages/settings/PipelineSettingsPage'
import { PluginsSettingsPage } from '@/pages/settings/PluginsSettingsPage'
import { ThemeSettingsPage } from '@/pages/settings/ThemeSettingsPage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useSchemaQuery } from '@/hooks/queries/useSchemaQuery'
import { KERNEL_NAV_ITEMS } from '@/services/settingsKernelNav'
import type { SettingsPanelEntry } from '@/services/schema/ContributionRegistry'

type NavKey =
  | 'kernel-theme'
  | 'kernel-plugins'
  | 'kernel-pipeline'
  | `plugin:${string}`
  | `declared:${string}`

interface NavItem {
  key: NavKey
  label: string
  group: '内核' | '插件'
  description?: string
  /** 条目前置图标（内核项为 emoji，来自 settingsKernelNav 数据源） */
  icon?: string
}

// 内核设置导航项统一来自共享数据源（与所有设置入口同源，避免散点双修）
const KERNEL_NAV: NavItem[] = KERNEL_NAV_ITEMS.map((item) => ({
  key: `kernel-${item.id}` as NavKey,
  label: item.label,
  group: item.group,
  description: item.description,
  icon: item.icon,
}))

/** 设置中枢 props：widgetRegistry 透传 tab.props，外部入口可带深链初始选中项 */
export interface SettingsHubWidgetProps {
  initialActive?: NavKey
}

/**
 * 设置中枢 — 内嵌设置导航 + 内容区
 */
export function SettingsHubWidget({ initialActive = 'kernel-plugins' }: SettingsHubWidgetProps) {
  const [active, setActive] = useState<NavKey>(initialActive)
  const [pluginPanels, setPluginPanels] = useState<SettingsPanelEntry[]>([])

  // schema（query 化）：设置中枢唯一 schema 消费端，共享缓存条目
  const schemaQuery = useSchemaQuery()

  useEffect(() => {
    if (!schemaQuery.data) return
    contributionRegistry.loadFromSchema(schemaQuery.data as unknown as Record<string, unknown>)
    setPluginPanels(contributionRegistry.getSettingsPanels())
  }, [schemaQuery.data])

  const pluginNav: NavItem[] = useMemo(
    () =>
      pluginPanels.flatMap((panel) =>
        panel.configFiles.map((file) => ({
          key: `plugin:${panel.pluginId}:${file.id}` as NavKey,
          label: file.label,
          group: '插件' as const,
          description: panel.pluginName,
        })),
      ),
    [pluginPanels],
  )

  // 插件直接声明的 settings 页（contributes.pages space=settings，非 config_files）
  const declaredPages = useMemo(
    () => contributionRegistry.getPagesBySpace('settings').filter((p) => !p.legacyFrom),
    // pluginPanels 在 loadFromSchema 后设置，作为「声明已加载」的触发器
    [pluginPanels],
  )
  const declaredNav: NavItem[] = useMemo(
    () =>
      declaredPages.map((p) => ({
        key: `declared:${p.id}` as NavKey,
        label: p.title ?? p.id,
        group: '插件' as const,
        description: '插件声明页',
      })),
    [declaredPages],
  )

  const activeDeclared = declaredPages.find((p) => `declared:${p.id}` === active)

  return (
    <div
      className="flex h-full min-h-0"
      style={{ background: 'var(--ds-bg-panel, hsl(var(--card)))' }}
      data-testid="settings-hub"
    >
      {/* 左导航树 */}
      <aside
        className="flex w-56 shrink-0 flex-col border-r"
        style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
      >
        <div className="border-b px-3 py-2.5" style={{ borderColor: 'var(--ds-border-subtle)' }}>
          <div className="text-foreground text-[13px] font-semibold">设置</div>
          <div className="text-muted-foreground font-mono text-[10px]">内核 · 插件</div>
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto p-2">
          <NavGroup
            icon={Settings}
            label="内核设置"
            hint="内核内置设置页"
            count={KERNEL_NAV.length}
            first
          >
            {KERNEL_NAV.map((item) => (
              <NavButton
                key={item.key}
                item={item}
                icon={
                  <span className="w-3.5 shrink-0 text-center text-[11px] leading-none">
                    {item.icon}
                  </span>
                }
                active={active === item.key}
                onClick={() => setActive(item.key)}
              />
            ))}
          </NavGroup>
          {declaredNav.length > 0 && (
            <NavGroup
              icon={LayoutGrid}
              label="插件页面"
              hint="插件声明的设置页（contributes.pages）"
              count={declaredNav.length}
            >
              {declaredNav.map((item) => (
                <NavButton
                  key={item.key}
                  item={item}
                  icon={<PluginIcon className="h-3.5 w-3.5 shrink-0" />}
                  active={active === item.key}
                  onClick={() => setActive(item.key)}
                />
              ))}
            </NavGroup>
          )}
          {pluginNav.length > 0 && (
            <NavGroup
              icon={FileCode}
              label="插件配置"
              hint="插件配置文件编辑"
              count={pluginNav.length}
            >
              {pluginNav.map((item) => (
                <NavButton
                  key={item.key}
                  item={item}
                  icon={<FileText className="h-3.5 w-3.5 shrink-0" />}
                  active={active === item.key}
                  onClick={() => setActive(item.key)}
                />
              ))}
            </NavGroup>
          )}
        </nav>
      </aside>

      {/* 右内容区：canvas 底色与左导航 panel 区分；内嵌现有设置页（无「设置总览」） */}
      <main className="min-h-0 min-w-0 flex-1 overflow-auto bg-[var(--ds-bg-canvas,hsl(var(--background)))]">
        {active === 'kernel-theme' && <ThemeSettingsPage />}
        {active === 'kernel-pipeline' && <PipelineSettingsPage embedded />}
        {active === 'kernel-plugins' && <PluginsSettingsPage />}
        {String(active).startsWith('plugin:') && (
          <PluginConfigEmbed pathKey={String(active).slice('plugin:'.length)} />
        )}
        {activeDeclared && (
          // 声明的 settings 页：经 PageRenderer 分发（widget/schema），声明驱动渲染
          <div className="h-full min-h-0">{renderPageContent(activeDeclared)}</div>
        )}
      </main>
    </div>
  )
}

/** 导航分组：区块头（图标 + 标题 + 计数 + 延伸线）+ 条目列；组间以细分隔线划区 */
function NavGroup({
  icon: GroupIcon,
  label,
  hint,
  count,
  first = false,
  children,
}: {
  icon: LucideIcon
  label: string
  hint: string
  count: number
  first?: boolean
  children: React.ReactNode
}) {
  return (
    <div
      className={cn(
        'border-[var(--ds-border-subtle,rgba(148,163,184,0.12))] py-2.5',
        !first && 'border-t',
      )}
      title={hint}
    >
      <div className="text-muted-foreground mb-1.5 flex items-center gap-1.5 px-1.5">
        <GroupIcon className="h-4 w-4 shrink-0 text-[var(--ds-accent-primary,#22D3EE)]" />
        <span className="text-foreground text-[13px] font-semibold leading-none">{label}</span>
        <div className="h-px flex-1 bg-[var(--ds-border-subtle,rgba(148,163,184,0.12))]" />
        <span className="font-mono text-[10px] leading-none">{count}</span>
      </div>
      <div className="flex flex-col gap-0.5">{children}</div>
    </div>
  )
}

function NavButton({
  item,
  icon,
  active,
  onClick,
}: {
  item: NavItem
  icon?: React.ReactNode
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-left text-[12px] transition-colors',
        active
          ? 'text-[var(--ds-accent-primary,#22D3EE)]'
          : 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]',
      )}
      style={
        active
          ? {
              background: 'var(--ds-bg-elevated, #111C38)',
              boxShadow: 'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))',
            }
          : undefined
      }
      title={item.description}
    >
      {icon}
      <span className="truncate">{item.label}</span>
    </button>
  )
}

function PluginConfigEmbed({ pathKey }: { pathKey: string }) {
  // pathKey = pluginId:fileId — 直接内联渲染 PluginConfigEditor（不跳转路由）
  const [pluginId, fileId] = pathKey.split(':')
  if (!pluginId || !fileId) {
    return <div className="text-muted-foreground p-4 text-sm">无效的配置路径: {pathKey}</div>
  }
  return <PluginConfigEditor key={pathKey} pluginId={pluginId} fileId={fileId} title={fileId} embedded />
}
