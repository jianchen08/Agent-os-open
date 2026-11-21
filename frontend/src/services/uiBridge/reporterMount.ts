/**
 * uiBridge reporter 生产挂载入口（AC-P1-7，ADR「纯函数+薄接线」的接线切片）
 *
 * 组装 P0 纯函数面（buildSidebarSnapshot + createReportScheduler）为生产上报器：
 * client_id 经 localStorage `ui_bridge_client_id` 稳定生成（§2.1）；挂载即首发；
 * WS 重连本地事件（GlobalWebSocket 'reconnected'）→ 立即上报；订阅面四源
 * （uiStore 视图态 / notificationStore 未读 / layoutModeStore 活动页签 /
 * queryKeys.sessions 会话列表缓存）变更触发防抖上报；dispose 全量拆除无泄漏。
 *
 * @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1生产接线 | @vision V2 全能闭环 | @ci frontend-test
 */
import { WS_LOCAL_EVENTS } from '@/constants/websocket'
import apiClient from '@/services/api/client'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { generateUUID } from '@/utils/uuid'
import { buildSidebarSnapshot } from './snapshotBuilder'
import { createReportScheduler } from './stateReporter'
import type { Session } from '@/types/models'

/** 本端稳定 client_id（localStorage 键，§2.1 冻结） */
export interface UiBridgeReporterHandle {
  getClientId(): string
  /** 拆除：停调度器 + 退订全部 store/WS/缓存订阅，无残留定时器 */
  dispose(): void
}

export interface ReporterMountOptions {
  /** 上报外发注入点（唯一 HTTP mock 点）；缺省 = 真实 POST /ext/ui_bridge/state */
  send?: (report: object) => Promise<void>
  /** 注入时钟（透传 createReportScheduler §2.3 契约位） */
  now?: () => number
  debounceMs?: number
  heartbeatMs?: number
}

const CLIENT_ID_STORAGE_KEY = 'ui_bridge_client_id'
const STATE_URL = '/ext/ui_bridge/state'
/** 与 FiveSpaceLayout panelWidth('sidebar', 248, ...) 同源的默认宽度（px） */
const SIDEBAR_DEFAULT_WIDTH_PX = 248
/** 与 panelWidth 容器宽度兜底同源（px） */
const CHAT_REGION_FALLBACK_WIDTH_PX = 1200

function ensureClientId(): string {
  try {
    const existing = localStorage.getItem(CLIENT_ID_STORAGE_KEY)
    if (existing) return existing
  } catch {
    // storage 不可用（隐私模式等）：降级为会话内稳定 id，不阻断上报
  }
  const id = `fe-${generateUUID()}`
  try {
    localStorage.setItem(CLIENT_ID_STORAGE_KEY, id)
  } catch {
    // 同上：写失败只影响跨刷新稳定，不影响本会话
  }
  return id
}

function unreadCountOf(state: { notifications: Array<{ isRead: boolean }> }): number {
  return state.notifications.filter((n) => !n.isRead).length
}

function activeTabIdOf(state: { workspaceTabs: Array<{ id: string; isActive: boolean }> }): string | null {
  return state.workspaceTabs.find((t) => t.isActive)?.id ?? null
}

function resolveWidthRatio(ratio: number | null): number {
  if (ratio !== null) return ratio
  const avail =
    typeof document === 'undefined'
      ? CHAT_REGION_FALLBACK_WIDTH_PX
      : (document.querySelector('[data-region="chat"]')?.getBoundingClientRect().width ??
        CHAT_REGION_FALLBACK_WIDTH_PX)
  return avail > 0 ? Math.min(1, SIDEBAR_DEFAULT_WIDTH_PX / avail) : SIDEBAR_DEFAULT_WIDTH_PX / CHAT_REGION_FALLBACK_WIDTH_PX
}

function buildReport(clientId: string): object {
  const ui = useUIStore.getState()
  const activeSessionId = useSessionStore.getState().activeSessionId
  const sessions = queryClient.getQueryData<Session[]>(queryKeys.sessions) ?? []
  const pluginPages = contributionRegistry
    .getPagesBySpace('workspace')
    .filter((p) => p.slot === 'activity-bar')
  const snapshot = buildSidebarSnapshot({
    collapsed: ui.sidebarCollapsed,
    widthRatio: resolveWidthRatio(ui.sidebarRatio),
    activeView: ui.sidebarActiveView,
    unreadCount: unreadCountOf(useNotificationStore.getState()),
    pluginPages,
    sessions: sessions.map((s) => ({
      id: s.id,
      title: s.title,
      pinned: s.pinned === true,
      active: s.id === activeSessionId,
    })),
  })
  return { client_id: clientId, region: 'sidebar', ts: Date.now(), ...snapshot }
}

/**
 * 挂载生产上报器：挂载即首发（不等防抖窗），此后四源变更防抖上报、
 * WS 重连立即上报、心跳周期保活。
 */
export function mountUiBridgeReporter(opts?: ReporterMountOptions): UiBridgeReporterHandle {
  const clientId = ensureClientId()
  const send = opts?.send ?? ((report: object) => apiClient.post(STATE_URL, report).then(() => undefined))
  // send 是唯一外发点（§2.3）：快照在挂载切片闭合，调度器只承载触发时机
  const scheduler = createReportScheduler({
    send: () => send(buildReport(clientId)),
    now: opts?.now ?? Date.now, // §2.3 契约位（调度器当前不读时刻，静态引用即可）
    debounceMs: opts?.debounceMs,
    heartbeatMs: opts?.heartbeatMs,
  })

  const unsubscribeUiStore = useUIStore.subscribe((state, prev) => {
    if (state.sidebarCollapsed !== prev.sidebarCollapsed || state.sidebarActiveView !== prev.sidebarActiveView) {
      scheduler.notifyChange()
    }
  })
  const unsubscribeNotifications = useNotificationStore.subscribe((state, prev) => {
    if (unreadCountOf(state) !== unreadCountOf(prev)) {
      scheduler.notifyChange()
    }
  })
  const unsubscribeLayout = useLayoutModeStore.subscribe((state, prev) => {
    if (activeTabIdOf(state) !== activeTabIdOf(prev)) {
      scheduler.notifyChange()
    }
  })
  const unsubscribeSessionsCache = queryClient.getQueryCache().subscribe((event) => {
    if (JSON.stringify(event.query.queryKey) === JSON.stringify(queryKeys.sessions)) {
      scheduler.notifyChange()
    }
  })
  const handleReconnect = (): void => {
    scheduler.onReconnect()
  }
  globalWS.subscribe(WS_LOCAL_EVENTS.RECONNECTED, handleReconnect)

  scheduler.onReconnect() // 挂载即首发（重连语义 = 立即上报，不等防抖窗）

  return {
    getClientId() {
      return clientId
    },
    dispose() {
      scheduler.stop()
      unsubscribeUiStore()
      unsubscribeNotifications()
      unsubscribeLayout()
      unsubscribeSessionsCache()
      globalWS.unsubscribe(WS_LOCAL_EVENTS.RECONNECTED, handleReconnect)
    },
  }
}
