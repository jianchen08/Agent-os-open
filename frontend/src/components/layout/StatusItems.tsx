/**
 * StatusItems · 插件状态项（task_layout_responsive 任务 2 + 模式体系 §5.0 通用能力）
 *
 * StatusBar 删除后，插件贡献的 dock 空间 + status 栏位项迁移到侧栏底部条带
 * （`sidebar-plugin-status`）。逻辑与原 StatusBar 一致：
 * - 来源：contributionRegistry.getPagesBySpace('dock') + slot==='status'，经 when 过滤
 *   （contributes.statusBarItems 归一化声明即现，禁用插件同源消失）
 * - 动态文案：widgetEventStore.latest[widget_id].data 优先，item.title 兜底
 * - onClick 行为声明：`{ type: 'navigate', page: <页面 id> }` → 点击 openPluginPage
 *   打开对应插件页面（目标页优先在声明者插件名下解析）；无声明/未知类型 = 纯展示
 * - 无项时不渲染（不占空间）
 */

import { useMemo } from 'react'
import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'
import { evaluateWhen, type ContextKeys } from '@/services/schema/whenExpression'
import { useContextKeys } from '@/stores/contextKeysStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { openPluginPage } from '@/services/workspacePanelOpener'

/** 状态项 onClick 行为声明（本期唯一行为类型：导航到页面） */
interface StatusBarClickBehavior {
  type: 'navigate'
  /** 导航目标页面 id（registry 内 PageDeclaration.id） */
  page: string
}

/**
 * 解析条目的 onClick 行为声明；缺省或非 navigate 类型返回 null（纯展示项）。
 */
export function parseStatusClickBehavior(item: PageDeclaration): StatusBarClickBehavior | null {
  const raw = item.onClick as { type?: unknown; page?: unknown } | undefined
  if (!raw || typeof raw !== 'object') return null
  if (raw.type !== 'navigate' || typeof raw.page !== 'string' || raw.page === '') return null
  return { type: 'navigate', page: raw.page }
}

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
  const behavior = parseStatusClickBehavior(item)

  const handleClick = () => {
    if (!behavior) return
    // 目标页优先在声明者插件名下解析（页面 id 命名空间归各插件所有，跨插件
    // 撞名不串页）；声明者名下未命中再按裸 id 全局查找
    const page =
      contributionRegistry.getPluginPages(item.pluginId ?? '').find((p) => p.id === behavior.page) ??
      contributionRegistry.getPage(behavior.page)
    if (!page) {
      useNotificationStore.getState().addNotification({
        title: '状态项无法打开页面',
        message: `状态项「${item.title ?? item.id}」声明的导航目标页 ${behavior.page} 不存在（提供它的插件可能已禁用）`,
        priority: 'normal',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 8000,
        sourceLabel: '状态栏',
      })
      return
    }
    openPluginPage(page)
  }

  const content = (
    <>
      {item.icon ? (
        <span className="shrink-0 text-[11px] leading-none" aria-hidden="true">
          {item.icon}
        </span>
      ) : (
        <span
          className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
          style={{ backgroundColor: color ?? 'var(--ds-status-pending, #94A3B8)' }}
        />
      )}
      <span className="text-muted-foreground truncate text-[11px] leading-none">{label}</span>
    </>
  )

  if (behavior) {
    return (
      <button
        type="button"
        onClick={handleClick}
        title={`${label}（点击打开 ${behavior.page}）`}
        data-testid={`status-item-nav-${item.id}`}
        className="hover:bg-accent flex min-w-0 cursor-pointer items-center gap-1.5 whitespace-nowrap rounded px-0.5 py-0.5 transition-colors"
      >
        {content}
      </button>
    )
  }

  return (
    <div className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
      {content}
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
