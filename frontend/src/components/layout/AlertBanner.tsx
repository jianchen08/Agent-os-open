/**
 * AlertBanner · 异常浮现提示条（task_layout_responsive 任务 2）
 *
 * 替代常驻 StatusBar：无常驻底栏，异常（连接断开 / 审批待处理 / 预算超限）时
 * 在顶栏下方浮现提示条，异常解除后延迟几秒自动收起。
 *
 * 样式参考 VS Code 顶部提示条：有事才出现，可点击跳转详情。
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { useBudgetStatus } from '@/hooks/useCostControl'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import type { BudgetStatusResponse } from '@/services/api/costControl'
import { cn } from '@/lib/utils'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 告警项 */
export interface AlertBannerItem {
  /** 唯一标识（同 id 去重） */
  id: string
  kind: 'connection' | 'approval' | 'budget'
  message: string
  tone?: 'error' | 'warning' | 'info'
  /** 点击横幅时的动作文案（如「查看监控」） */
  actionLabel?: string
}

/** 异常解除后横幅保留时长（提示"已恢复"再消失） */
const DEFAULT_RESOLVE_HOLD_MS = 4000

interface AlertBannerProps {
  /** 当前活跃告警（由 useLayoutAlerts 或上层传入） */
  alerts: AlertBannerItem[]
  /** 点击横幅回调（跳转详情 / 处理动作） */
  onAction?: (item: AlertBannerItem) => void
  /** 异常解除后保留时长 ms（测试可注入） */
  resolveHoldMs?: number
}

const TONE_STYLE: Record<NonNullable<AlertBannerItem['tone']>, string> = {
  error: 'border-status-error/40 bg-status-error/10 text-status-error',
  warning: 'border-status-waiting/40 bg-status-waiting/10 text-status-waiting',
  info: 'border-primary/40 bg-primary/10 text-primary',
}

/**
 * 异常浮现提示条。
 *
 * 内部维护 items 表：告警出现 → 立即浮现；告警从 alerts 消失 → 保留
 * resolveHoldMs（提示"已恢复"）后自动移除。同 id 去重、更新即刷新文案。
 */
export function AlertBanner({
  alerts,
  onAction,
  resolveHoldMs = DEFAULT_RESOLVE_HOLD_MS,
}: AlertBannerProps) {
  const [items, setItems] = useState<Record<string, AlertBannerItem>>(() =>
    Object.fromEntries(alerts.map((a) => [a.id, a])),
  )
  const holdTimersRef = useRef(new Set<ReturnType<typeof setTimeout>>())

  const alertIds = useMemo(() => new Set(alerts.map((a) => a.id)), [alerts])

  // 新告警 upsert（出现立即浮现；同 id 更新文案）
  useEffect(() => {
    setItems((prev) => {
      let changed = false
      const next = { ...prev }
      for (const a of alerts) {
        if (next[a.id] !== a) {
          next[a.id] = a
          changed = true
        }
      }
      return changed ? next : prev
    })
  }, [alerts])

  // 已解除的告警：延迟 resolveHoldMs 后移除（异常解除后几秒自动收起）
  useEffect(() => {
    for (const timer of holdTimersRef.current) clearTimeout(timer)
    holdTimersRef.current.clear()

    for (const [id] of Object.entries(items)) {
      if (alertIds.has(id)) continue
      const timer = setTimeout(() => {
        holdTimersRef.current.delete(timer)
        setItems((prev) => {
          if (!prev[id]) return prev
          const next = { ...prev }
          delete next[id]
          return next
        })
      }, resolveHoldMs)
      holdTimersRef.current.add(timer)
    }

    return () => {
      for (const timer of holdTimersRef.current) clearTimeout(timer)
      holdTimersRef.current.clear()
    }
  }, [items, alertIds, resolveHoldMs])

  // 渲染顺序：活跃告警在前，已解除（保留期内）的在后
  const resolvedItems = Object.entries(items)
    .filter(([id]) => !alertIds.has(id))
    .map(([, item]) => item)
  const visible = [...alerts, ...resolvedItems]
  if (visible.length === 0) return null

  return (
    <div className="flex shrink-0 flex-col gap-1 px-2 pt-1" role="region" aria-label="系统异常提示">
      {visible.map((item) => (
        <button
          key={item.id}
          type="button"
          role="alert"
          onClick={() => onAction?.(item)}
          className={cn(
            'flex min-h-9 w-full items-center gap-2 rounded-md border px-3 py-1.5 text-left text-xs',
            TONE_STYLE[item.tone ?? 'info'],
            onAction && 'cursor-pointer',
          )}
          data-testid="alert-banner"
        >
          <span className="min-w-0 flex-1 truncate">{item.message}</span>
          {item.actionLabel && onAction && (
            <span className="shrink-0 font-medium underline-offset-2 hover:underline">
              {item.actionLabel}
            </span>
          )}
        </button>
      ))}
    </div>
  )
}

/**
 * budget 告警派生：alert_level ∈ {warning, critical, exhausted} 时出一条
 * （critical/exhausted → error tone），info 不出。
 * 后端 budget_manager 已按 daily/monthly 取 max 判定单值 alert_level——
 * 前端只出一条（id=budget，升级时同 id 覆盖），不双条刷屏。
 */
function deriveBudgetAlert(status: BudgetStatusResponse | null): AlertBannerItem | null {
  const level = status?.alert_level
  if (!status) return null
  if (level !== 'warning' && level !== 'critical' && level !== 'exhausted') return null
  const percent = Math.round(status.usage_percent)
  if (level === 'exhausted') {
    return {
      id: 'budget',
      kind: 'budget',
      tone: 'error',
      message: `预算已耗尽（使用率 ${percent}%），任务执行可能被拦截`,
      actionLabel: '查看成本',
    }
  }
  if (level === 'critical') {
    return {
      id: 'budget',
      kind: 'budget',
      tone: 'error',
      message: `预算使用已达 ${percent}%，超过严重阈值`,
      actionLabel: '查看成本',
    }
  }
  return {
    id: 'budget',
    kind: 'budget',
    tone: 'warning',
    message: `预算使用已达 ${percent}%，接近限额`,
    actionLabel: '查看成本',
  }
}

/**
 * 从 layoutModeStore + cost_control 派生系统告警：
 * - connection：断开/失败（重连中不打扰）
 * - approval：审批待处理
 * - budget：预算超限（真源 cost_control /ext/cost_control/budget/status 的
 *   alert_level；monitoring 插件 token 累计恒 0 不可用——勿接错源）
 */
export function useLayoutAlerts(): AlertBannerItem[] {
  const connectionStatus = useLayoutModeStore((s) => s.connectionStatus)
  const pendingInteractions = useLayoutModeStore((s) => s.pendingInteractions)
  const { budgetStatus, refetch } = useBudgetStatus()

  // cost_update 事件到达时复查预算状态（事件驱动，免轮询）；
  // 失败静默——告警以最后一次已知状态为准
  useEffect(() => {
    const refetchBudget = () => {
      refetch().catch(() => undefined)
    }
    globalWS.subscribe(WS_SERVER_EVENTS.COST_UPDATE, refetchBudget)
    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.COST_UPDATE, refetchBudget)
    }
  }, [refetch])

  return useMemo(() => {
    const items: AlertBannerItem[] = []
    if (connectionStatus.state === 'disconnected' || connectionStatus.state === 'failed') {
      items.push({
        id: 'connection',
        kind: 'connection',
        tone: 'error',
        message:
          connectionStatus.state === 'failed'
            ? '内核连接失败，请检查内核是否运行'
            : '内核连接已断开，正在尝试恢复…',
        actionLabel: '查看监控',
      })
    }
    if (pendingInteractions.length > 0) {
      items.push({
        id: 'approval',
        kind: 'approval',
        tone: 'warning',
        message: `有 ${pendingInteractions.length} 项审批待处理`,
        actionLabel: '去处理',
      })
    }
    const budgetAlert = deriveBudgetAlert(budgetStatus)
    if (budgetAlert) items.push(budgetAlert)
    return items
  }, [connectionStatus.state, pendingInteractions.length, budgetStatus])
}
