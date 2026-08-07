/**
 * 设置页 — 左列表 + 右内联编辑
 *
 * 去掉"设置中心"卡片二次跳转。点击左侧模块配置项，右侧直接显示并修改。
 * 插件配置走 0.2 /api/v1/plugins/{id}/config/{file_id}。
 */

import { KERNEL_NAV_ITEMS } from '@/services/settingsKernelNav'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { getSchema } from '@/services/api/schema'
import type { SettingsPanelEntry } from '@/services/schema/ContributionRegistry'
import { PluginConfigEditor } from '@/components/config/PluginConfigEditor'
import { PluginsSettingsPage } from '@/pages/settings/PluginsSettingsPage'
import { ThemeSettingsPage } from '@/pages/settings/ThemeSettingsPage'
import { PipelineSettingsPage } from '@/pages/settings/PipelineSettingsPage'
import { ApiSettingsPage } from '@/pages/settings/ApiSettingsPage'
import { LlmSettingsPage } from '@/pages/settings/LlmSettingsPage'
import { ContextWindowSettingsPage } from '@/pages/settings/ContextWindowSettingsPage'
import { ConcurrencySettingsPage } from '@/pages/settings/ConcurrencySettingsPage'
import { CostSettingsPage } from '@/pages/settings/CostSettingsPage'

/** 左侧导航条目 */
type NavItem =
  | {
      kind: 'builtin'
      id: string
      title: string
      description: string
      icon: string
    }
  | {
      kind: 'plugin'
      id: string
      title: string
      description: string
      icon: string
      pluginId: string
      fileId: string
      pluginName: string
    }

// 内核设置导航项统一来自共享数据源（与 SettingsHubWidget.KERNEL_NAV 同源，避免散点双修）
const BUILTIN_ITEMS: Extract<NavItem, { kind: 'builtin' }>[] = KERNEL_NAV_ITEMS.map(
  (item) => ({
    kind: 'builtin',
    id: item.id,
    title: item.title,
    description: item.description,
    icon: item.icon,
  }),
)

/** 设置页主组件：左导航右编辑 */
export function SettingsPage() {
  const [settingsPanels, setSettingsPanels] = useState<SettingsPanelEntry[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string>(BUILTIN_ITEMS[0].id)

  useEffect(() => {
    let cancelled = false
    setIsLoading(true)

    getSchema()
      .then((schema) => {
        if (cancelled) return
        contributionRegistry.loadFromSchema(schema as unknown as Record<string, unknown>)
        setSettingsPanels(contributionRegistry.getSettingsPanels())
      })
      .catch(() => {
        // schema 失败时仍展示内置项
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  const pluginItems: NavItem[] = useMemo(() => {
    const items: NavItem[] = []
    for (const panel of settingsPanels) {
      for (const file of panel.configFiles) {
        items.push({
          kind: 'plugin',
          id: `plugin:${panel.pluginId}:${file.id}`,
          title: file.label,
          description: panel.pluginName,
          icon: panel.pluginIcon || '⚙️',
          pluginId: panel.pluginId,
          fileId: file.id,
          pluginName: panel.pluginName,
        })
      }
    }
    return items
  }, [settingsPanels])

  const allItems = useMemo(() => [...BUILTIN_ITEMS, ...pluginItems], [pluginItems])
  const selected = allItems.find((item) => item.id === selectedId) ?? allItems[0]

  return (
    <div
      className="text-foreground flex h-screen flex-col overflow-hidden"
      style={{ background: 'var(--ds-bg-canvas, #04060F)' }}
    >
      <header
        className="flex h-12 shrink-0 items-center border-b px-4"
        style={{
          background: 'var(--ds-bg-panel, #0A1226)',
          borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
        }}
      >
        <Link to="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </Link>
        <h1 className="ml-4 text-base font-semibold">设置</h1>
        <span className="text-muted-foreground ml-auto font-mono text-[10px]">Deep Space v2</span>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 左侧模块列表 */}
        <aside
          className="w-64 shrink-0 overflow-y-auto border-r p-3 sm:w-72"
          style={{
            background: 'var(--ds-bg-panel, #0A1226)',
            borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
          }}
        >
          <SectionTitle>内核设置</SectionTitle>
          <nav className="mb-4 space-y-1">
            {BUILTIN_ITEMS.map((item) => (
              <NavButton
                key={item.id}
                item={item}
                active={selected?.id === item.id}
                onClick={() => setSelectedId(item.id)}
              />
            ))}
          </nav>

          <SectionTitle>插件配置</SectionTitle>
          {isLoading && (
            <div className="text-muted-foreground px-2 py-3 text-xs">加载插件配置...</div>
          )}
          {!isLoading && pluginItems.length === 0 && (
            <div className="text-muted-foreground px-2 py-3 text-xs">暂无插件配置项</div>
          )}
          <nav className="space-y-1">
            {pluginItems.map((item) => (
              <NavButton
                key={item.id}
                item={item}
                active={selected?.id === item.id}
                onClick={() => setSelectedId(item.id)}
              />
            ))}
          </nav>
        </aside>

        {/* 右侧内联编辑区（嵌入子页时隐藏其独立全屏头，避免双层导航） */}
        <main className="min-w-0 flex-1 overflow-y-auto p-4 sm:p-6">
          {selected?.kind === 'plugin' && (
            <PluginConfigEditor
              key={selected.id}
              pluginId={selected.pluginId}
              fileId={selected.fileId}
              title={selected.title}
              embedded
            />
          )}
          {selected?.kind === 'builtin' && (
            <div className="h-full min-h-0 [&>div]:!h-auto [&>div]:!min-h-0 [&>div]:!overflow-visible [&_header]:!hidden">
              {selected.id === 'theme' && <ThemeSettingsPage embedded />}
              {selected.id === 'pipeline' && <PipelineSettingsPage embedded />}
              {selected.id === 'api' && <ApiSettingsPage />}
              {selected.id === 'llm' && <LlmSettingsPage embedded />}
              {selected.id === 'context' && <ContextWindowSettingsPage />}
              {selected.id === 'concurrency' && <ConcurrencySettingsPage />}
              {selected.id === 'cost' && <CostSettingsPage />}
              {selected.id === 'plugins' && (
                <PluginsSettingsPage
                  embedded
                  onSelectPluginConfig={(pluginId, fileId) => {
                    setSelectedId(`plugin:${pluginId}:${fileId}`)
                  }}
                />
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  )
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-muted-foreground mb-2 px-2 text-[10px] font-medium tracking-wide uppercase">
      {children}
    </h2>
  )
}

function NavButton({
  item,
  active,
  onClick,
}: {
  item: NavItem
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex w-full items-start gap-2 rounded-lg px-2.5 py-2 text-left transition-colors ${
        active
          ? 'bg-[var(--ds-bg-hover,#1A2748)] text-foreground'
          : 'text-muted-foreground hover:bg-[var(--ds-bg-hover,#1A2748)] hover:text-foreground'
      }`}
    >
      <span className="mt-0.5 text-base leading-none">{item.icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-medium">{item.title}</span>
      </span>
    </button>
  )
}
