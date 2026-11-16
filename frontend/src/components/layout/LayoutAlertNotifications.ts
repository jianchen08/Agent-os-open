/**
 * 系统异常通知 · 通知中心承载（原 AlertBanner 顶栏横幅并入右侧实时通知面）
 *
 * 无独立顶栏横幅：连接断开 / 审批待处理 / 预算超限三类系统异常经
 * useConditionNotifications 同步进通知中心（错误=红色 critical 条目，
 * 警示=high 条目，X 可关闭；动作按钮复用通知条目 actions 面）。条件解除
 * 后条目延迟几秒自动移除（缓冲断连抖动）。
 *
 * 数据源与派生口径承自原 AlertBanner（useLayoutAlerts 不变）：
 * - connection：断开/失败；重连中显示「正在重连（第 N 次）」动态进度，
 *   连续失败达 RECONNECT_DEGRADE_ATTEMPTS 降级为「多次重连失败 + 立即重连」
 *   （自动重连不停止，一键重连是唯一用户干预形态）
 * - approval：审批待处理
 * - budget：预算超限（真源 cost_control 插件 budget/status 的
 *   alert_level，端点面=endpoints.generated.ts 声明投影；monitoring 插件
 *   token 累计恒 0 不可用——勿接错源）。导航动作=监控页声明引用
 *   （BUDGET_PAGE_ACTION，经注册表活值解析）
 */
import { useEffect, useMemo } from 'react'
import { RECONNECT_DEGRADE_ATTEMPTS, WS_SERVER_EVENTS } from '@/constants/websocket'
import { useConditionNotifications, type ConditionNotification } from '@/hooks/useConditionNotifications'
import { useBudgetStatus } from '@/hooks/useCostControl'
import {
  PLUGIN_DECLARED_PAGES,
  type DeclaredPluginPageRef,
} from '@/services/schema/pluginPages.generated'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { openDeclaredPluginPage } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { BudgetStatusResponse } from '@/services/api/costControl'
import type { NotificationAction, NotificationPriority } from '@/types/notification'

/** 告警项 */
export interface LayoutAlertItem {
  /** 唯一标识（同 id 去重） */
  id: string
  kind: 'connection' | 'approval' | 'budget'
  message: string
  tone?: 'error' | 'warning' | 'info'
  /** 点击横幅时的动作文案（如「查看监控」） */
  actionLabel?: string
  /**
   * 告警动作（通知条目动作按钮按此执行；缺省无导航动作）：
   * - reconnect：一键重连（WS 降级态）
   * - open-page：按 pageRef 声明引用打开插件页面（声明键出自
   *   pluginPages.generated.ts 投影，运行时经注册表活值解析，E6 声明面化）
   */
  action?: 'reconnect' | 'open-page'
  /** action='open-page' 的目标声明页引用 */
  pageRef?: DeclaredPluginPageRef
}

/**
 * budget 告警的声明动作（单点）：成本看板并入监控页（cost_control 无自有页面，
 * ui_schema 成本卡挂在 monitoring 页组台），导航目标=监控页声明引用——
 * 声明键出自 pluginPages.generated.ts 投影，插件改页面声明随闸随动。
 */
const BUDGET_PAGE_ACTION = {
  action: 'open-page',
  pageRef: PLUGIN_DECLARED_PAGES.monitoring.monitoring,
} as const

/**
 * budget 告警派生：alert_level ∈ {warning, critical, exhausted} 时出一条
 * （critical/exhausted → error tone），info 不出。
 * 后端 budget_manager 已按 daily/monthly 取 max 判定单值 alert_level——
 * 前端只出一条（id=budget，升级时同 id 覆盖），不双条刷屏。
 */
function deriveBudgetAlert(status: BudgetStatusResponse | null): LayoutAlertItem | null {
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
      ...BUDGET_PAGE_ACTION,
    }
  }
  if (level === 'critical') {
    return {
      id: 'budget',
      kind: 'budget',
      tone: 'error',
      message: `预算使用已达 ${percent}%，超过严重阈值`,
      actionLabel: '查看成本',
      ...BUDGET_PAGE_ACTION,
    }
  }
  return {
    id: 'budget',
    kind: 'budget',
    tone: 'warning',
    message: `预算使用已达 ${percent}%，接近限额`,
    actionLabel: '查看成本',
    ...BUDGET_PAGE_ACTION,
  }
}

/**
 * 从 layoutModeStore + cost_control 派生系统告警（条件同步的数据源）。
 */
export function useLayoutAlerts(): LayoutAlertItem[] {
  const connectionStatus = useLayoutModeStore((s) => s.connectionStatus)
  const pendingInteractions = useLayoutModeStore((s) => s.pendingInteractions)
  const { budgetStatus, refetch: refreshBudget } = useBudgetStatus()

  // cost_update 事件到达时复查预算状态（事件驱动，免轮询）；
  // 失败静默——告警以最后一次已知状态为准（useBudgetStatus 内 error 态承载）
  useEffect(() => {
    const refetchBudget = () => {
      refreshBudget().catch(() => {
        // HACK: WS 回调无人消费 rejection，兜底防 unhandled rejection；
        // 失败态由 useBudgetStatus 的 error 状态承载（OBS-R258-1 吞错误规则登记）
      })
    }
    globalWS.subscribe(WS_SERVER_EVENTS.COST_UPDATE, refetchBudget)
    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.COST_UPDATE, refetchBudget)
    }
  }, [refreshBudget])

  return useMemo(() => {
    const items: LayoutAlertItem[] = []
    if (connectionStatus.state === 'reconnecting') {
      const attempt = connectionStatus.reconnectAttempt
      if (attempt >= RECONNECT_DEGRADE_ATTEMPTS) {
        items.push({
          id: 'connection',
          kind: 'connection',
          tone: 'error',
          message: `多次重连失败（已尝试 ${attempt} 次），自动重连仍在继续`,
          actionLabel: '立即重连',
          action: 'reconnect',
        })
      } else {
        items.push({
          id: 'connection',
          kind: 'connection',
          tone: 'warning',
          message: `连接断开，正在重连（第 ${attempt} 次）…`,
        })
      }
    } else if (connectionStatus.state === 'disconnected' || connectionStatus.state === 'failed') {
      items.push({
        id: 'connection',
        kind: 'connection',
        tone: 'error',
        message:
          connectionStatus.state === 'failed'
            ? '内核连接失败，请检查内核是否运行'
            : '内核连接已断开，正在尝试恢复…',
        actionLabel: '查看监控',
        action: 'open-page',
        pageRef: PLUGIN_DECLARED_PAGES.monitoring.monitoring,
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
  }, [connectionStatus.state, connectionStatus.reconnectAttempt, pendingInteractions.length, budgetStatus])
}

/** 通知条目动作按钮携带的告警动作载荷（NotificationCenter 据此分发执行） */
export interface LayoutAlertActionPayload {
  action?: LayoutAlertItem['action']
  pageRef?: LayoutAlertItem['pageRef']
}

/** tone → 通知优先级（error=红色 critical；其余警示档=high） */
function priorityForTone(tone: LayoutAlertItem['tone']): NotificationPriority {
  return tone === 'error' ? 'critical' : 'high'
}

/** tone → 通知分类（error 分类=红/警示图标面；warning 走 alert 分类） */
function categoryForTone(tone: LayoutAlertItem['tone']): 'error' | 'alert' {
  return tone === 'error' ? 'error' : 'alert'
}

/**
 * 告警项 → 通知条目载荷（合并一核心映射：错误样式/动作按钮在此定形）。
 * 动作经 actions 面按钮执行（payload.layoutAlert 由 NotificationCenter
 * 分发到 runLayoutAlertAction）；无动作项无按钮。
 */
export function layoutAlertToNotificationInput(
  item: LayoutAlertItem,
): ConditionNotification['notification'] {
  const actions: NotificationAction[] | undefined = item.actionLabel
    ? [
        {
          id: `${item.id}-action`,
          label: item.actionLabel,
          action: 'custom',
          payload: { layoutAlert: { action: item.action, pageRef: item.pageRef } },
        },
      ]
    : undefined
  return {
    title: item.message,
    category: categoryForTone(item.tone),
    priority: priorityForTone(item.tone),
    isBlocking: false,
    sourceLabel: '系统',
    ...(actions ? { actions } : {}),
  }
}

/**
 * 告警动作执行器（通知条目动作按钮的落点）：reconnect 一键重连；
 * open-page 经声明注册表开页（pageRef 出自 pluginPages.generated.ts 投影，
 * 插件改页面声明重生成后导航随动）；无动作条目（审批计数提示）零动作。
 */
export function runLayoutAlertAction(payload: LayoutAlertActionPayload): void {
  if (payload.action === 'reconnect') {
    globalWS.forceReconnect()
    return
  }
  if (payload.action === 'open-page' && payload.pageRef) {
    openDeclaredPluginPage(payload.pageRef)
  }
}

/** 告警项 → 条件通知描述（kind 承载 episode 命名空间） */
function toCondition(item: LayoutAlertItem): ConditionNotification {
  return { kind: item.kind, notification: layoutAlertToNotificationInput(item) }
}

/**
 * 系统异常 → 通知中心同步（合并一挂载点）：主布局常驻调用，条件集变化
 * 自动对账。条目呈现/关闭走通知中心现有面，本钩子零 DOM。
 */
export function useLayoutAlertNotifications(resolveHoldMs?: number): void {
  const alerts = useLayoutAlerts()
  const conditions = useMemo(() => alerts.map(toCondition), [alerts])
  useConditionNotifications(conditions, resolveHoldMs)
}
