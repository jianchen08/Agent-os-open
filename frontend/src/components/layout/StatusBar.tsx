/**
 * StatusBar · Deep Space v2 App Shell 底栏
 *
 * 高度 22px：左组系统状态 + 右组成本/时间
 * 模型与上下文用量只在输入框展示，不在此栏
 */

import { Moon } from '@/assets/icons'
import { useEffect, useMemo, useState } from 'react'
import { useCostControl } from '@/hooks/useCostControl'
import { cn } from '@/lib/utils'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'
import { evaluateWhen } from '@/services/schema/whenExpression'
import { useContextKeys } from '@/stores/contextKeysStore'

export interface StatusBarProps {
  /** 本轮 token / 成本文案 */
  costLabel?: string
  className?: string
}

/**
 * 底部状态栏：连接 / 管道 / 审批 + 插件贡献项 + 成本 + 时间
 *
 * 系统状态（连接/管道/审批）来自 layoutModeStore；插件贡献项来自
 * contributionRegistry.getPagesBySpace('dock') 的 status 栏位页（经 when 过滤），
 * 其动态文案来自对应 widget_id 的 widgetEventStore.latest。
 */
export function StatusBar({ costLabel, className }: StatusBarProps) {
  const connectionStatus = useLayoutModeStore((s) => s.connectionStatus)
  const activeExecutions = useLayoutModeStore((s) => s.activeExecutions)
  const pendingInteractions = useLayoutModeStore((s) => s.pendingInteractions)
  const [now, setNow] = useState(() => formatTime(new Date()))
  const { usageStats, fetchUsageStatistics } = useCostControl()
  const contextKeys = useContextKeys((s) => s.keys)

  useEffect(() => {
    const timer = window.setInterval(() => setNow(formatTime(new Date())), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    void fetchUsageStatistics().catch(() => {})
  }, [fetchUsageStatistics])

  const resolvedCostLabel = useMemo(() => {
    if (costLabel) return costLabel
    const g = usageStats?.global_stats
    if (!g) return undefined
    const cost = g.estimated_daily_cost ?? 0
    const tok = g.daily_tokens ?? 0
    const cny = cost * 7.2
    const costText = cny < 0.01 ? `¥${cny.toFixed(4)}` : `¥${cny.toFixed(3)}`
    return `${costText} / ${tok.toLocaleString()} tok`
  }, [costLabel, usageStats])

  const connected = connectionStatus.state === 'connected'
  const connectionLabel = {
    connected: '内核已连接',
    connecting: '连接中…',
    reconnecting: '重连中…',
    disconnected: '未连接',
    failed: '连接失败',
  }[connectionStatus.state]

  const running = activeExecutions.find((e) => e.status === 'running')
  const pipelineLabel = running
    ? `管道执行中 · ${running.name || running.type || running.id}`
    : '管道空闲'

  // 插件贡献的状态栏项（dock 空间 + status 栏位，经 when 过滤可见性）
  const pluginItems = useMemo(
    () =>
      contributionRegistry
        .getPagesBySpace('dock')
        .filter((p) => p.slot === 'status')
        .filter((item) => evaluateWhen(item.when, contextKeys)),
    [contextKeys],
  )

  return (
    <footer
      className={cn(
        'border-border flex shrink-0 items-center justify-between border-t px-2.5',
        className,
      )}
      style={{
        height: 'var(--layout-statusbar-height, 22px)',
        background: 'var(--ds-bg-panel, hsl(var(--card)))',
      }}
      data-testid="status-bar"
    >
      <div className="flex min-w-0 flex-1 items-center gap-4 overflow-hidden">
        <StatusItem
          color={connected ? 'var(--ds-status-success, #34D399)' : 'var(--ds-status-error, #F87171)'}
          label={connectionLabel}
        />
        <StatusItem
          color={
            running
              ? 'var(--ds-status-running, #22D3EE)'
              : 'var(--ds-status-pending, #94A3B8)'
          }
          label={pipelineLabel}
        />
        {pendingInteractions.length > 0 && (
          <StatusItem
            color="var(--ds-status-waiting, #FBBF24)"
            label={`待审批 ${pendingInteractions.length}`}
          />
        )}
        {pluginItems.map((item) => (
          <PluginStatusItem key={item.id} item={item} />
        ))}
      </div>

      <div className="text-muted-foreground flex shrink-0 items-center gap-3 font-mono text-[10px]">
        {resolvedCostLabel && <span>{resolvedCostLabel}</span>}
        <Moon className="h-3.5 w-3.5 opacity-70" aria-hidden />
        <span>{now}</span>
      </div>
    </footer>
  )
}

/**
 * 插件贡献的状态栏项：动态文案优先取 widgetEventStore.latest.data，
 * 兜底用 item.title。
 */
function PluginStatusItem({ item }: { item: PageDeclaration }) {
  // 订阅该 item 的 widget_id 的最新事件（若有 widget 字段则用它，否则用 item.id）
  const widgetId = item.widget ?? item.id
  const latest = useWidgetEventStore((s) => s.latest[widgetId])
  const label = useMemo(() => resolvePluginLabel(item, latest), [item, latest])
  const color = (item.props as { color?: string } | undefined)?.color
  return <StatusItem color={color ?? 'var(--ds-status-pending, #94A3B8)'} label={label} />
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

function StatusItem({ color, label }: { color: string; label: string }) {
  return (
    <div className="flex items-center gap-1.5 whitespace-nowrap">
      <span
        className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
        style={{ backgroundColor: color }}
      />
      <span className="text-muted-foreground text-[11px] leading-none">{label}</span>
    </div>
  )
}

function formatTime(d: Date): string {
  const h = String(d.getHours()).padStart(2, '0')
  const m = String(d.getMinutes()).padStart(2, '0')
  return `${h}:${m}`
}
