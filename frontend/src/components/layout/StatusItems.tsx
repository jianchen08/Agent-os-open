/**
 * StatusItems · 插件状态项（task_layout_responsive 任务 2）
 *
 * StatusBar 删除后，插件贡献的 dock 空间 + status 栏位项迁移到侧栏底部条带
 * （`sidebar-plugin-status`）。逻辑与原 StatusBar 一致：
 * - 来源：contributionRegistry.getPagesBySpace('dock') + slot==='status'，经 when 过滤
 * - 动态文案：widgetEventStore.latest[widget_id].data 优先，item.title 兜底
 * - 无项时不渲染（不占空间）
 */

import { useMemo } from 'react'
import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'
import { evaluateWhen, type ContextKeys } from '@/services/schema/whenExpression'
import { useContextKeys } from '@/stores/contextKeysStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'

/**
 * 从注册表解析插件状态项（dock/status，经 when 过滤）。
 * 导出为纯函数便于测试与复用。
 */
export function resolvePluginStatusItems(contextKeys: Record<string, unknown>): PageDeclaration[] {
  return contributionRegistry
    .getPagesBySpace('dock')
    .filter((p) => p.slot === 'status')
    .filter((item) => evaluateWhen(item.when, contextKeys as ContextKeys))
}

/**
 * 插件状态项条带（挂载于侧栏底部）：
 * 动态文案优先取 widgetEventStore.latest.data，兜底用 item.title。
 */
export function PluginStatusItems() {
  const contextKeys = useContextKeys((s) => s.keys)

  const items = useMemo(() => resolvePluginStatusItems(contextKeys), [contextKeys])
  if (items.length === 0) return null

  return (
    <div
      className="border-border/50 flex flex-wrap items-center gap-x-3 gap-y-1 border-t px-3 py-1"
      data-testid="sidebar-plugin-status"
    >
      {items.map((item) => (
        <PluginStatusItem key={item.id} item={item} />
      ))}
    </div>
  )
}

/** 单条插件状态项：订阅该 item 的 widget_id 的最新事件（若有 widget 字段则用它，否则用 item.id） */
function PluginStatusItem({ item }: { item: PageDeclaration }) {
  const widgetId = item.widget ?? item.id
  const latest = useWidgetEventStore((s) => s.latest[widgetId])
  const label = useMemo(() => resolvePluginLabel(item, latest), [item, latest])
  const color = (item.props as { color?: string } | undefined)?.color

  return (
    <div className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
      <span
        className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
        style={{ backgroundColor: color ?? 'var(--ds-status-pending, #94A3B8)' }}
      />
      <span className="text-muted-foreground truncate text-[11px] leading-none">{label}</span>
    </div>
  )
}

/** 从 item + latest 事件解析显示文案：latest.data 优先，item.title 兜底。 */
function resolvePluginLabel(
  item: PageDeclaration,
  latest: { data?: Record<string, unknown> } | undefined,
): string {
  if (latest?.data) {
    // 常见字段优先级：label/title/text/value
    const d = latest.data
    const picked =
      (d.label as string | undefined) ??
      (d.title as string | undefined) ??
      (d.text as string | undefined) ??
      (typeof d.value === 'number' ? String(d.value) : (d.value as string | undefined))
    if (picked) {
      const prefix = item.title ? `${item.title}: ` : ''
      return `${prefix}${picked}`
    }
  }
  return item.title ?? item.id
}
