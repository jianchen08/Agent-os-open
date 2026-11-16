/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：系统异常 → 通知中心同步（原 AlertBanner 顶栏横幅并入右侧实时通知面）
 * + useLayoutAlerts（task_layout_responsive 任务 2 派生口径）
 *
 * 合并改造契约：
 * - 无独立顶栏横幅（DOM 面已删）；告警经 useConditionNotifications 同步进
 *   通知中心条目（错误=critical 红条目、警示=high 条目，X 可关闭）
 * - 条件解除后条目延迟 4s 自动移除；保留期内复燃沿用同一条目
 * - 用户关闭后同轮不回弹；恢复后再触发以新 episode 重报
 * - 动作按钮（立即重连/查看监控/查看成本）经 payload 分发 runLayoutAlertAction
 * - useLayoutAlerts 从 layoutModeStore 派生告警项（口径不变）
 * - budget：cost_control /budget/status alert_level 过阈出现（治理债清理 7.5.3）
 */

import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RECONNECT_DEGRADE_ATTEMPTS, WS_SERVER_EVENTS } from '@/constants/websocket'
import { PLUGIN_DECLARED_PAGES } from '@/services/schema/pluginPages.generated'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { openDeclaredPluginPage } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import {
  resetDismissedNotificationIdsForTest,
  useNotificationStore,
} from '@/stores/notificationStore'
import {
  layoutAlertToNotificationInput,
  runLayoutAlertAction,
  useLayoutAlertNotifications,
  useLayoutAlerts,
  type LayoutAlertItem,
} from '../LayoutAlertNotifications'
import type { BudgetStatusResponse } from '@/services/api/costControl'
import type * as costControlMod from '@/services/api/costControl'

// useLayoutAlerts 的 budget 源是 cost_control getBudgetStatus（经 useBudgetStatus）。
// mock 掉网络层，用可变 holder 控制各用例的 alert_level。
const mockBudget = vi.hoisted(
  () => ({ current: null as costControlMod.BudgetStatusResponse | null }),
)
vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus: async () => mockBudget.current,
}))
vi.mock('@/services/workspacePanelOpener', () => ({
  openDeclaredPluginPage: vi.fn(),
}))

const CONNECTED = {
  state: 'connected' as const,
  latencyMs: 5,
  reconnectAttempt: 0,
  lastConnectedAt: null,
  queuedMessages: 0,
}

function makeAlert(over: Partial<LayoutAlertItem> = {}): LayoutAlertItem {
  return { id: 'conn', kind: 'connection', message: '内核连接已断开', tone: 'error', ...over }
}

/** 构造 BudgetStatus（alert_level 判定在后端 budget_manager，前端按值消费） */
function makeBudget(alert_level: string, usage_percent = 85): BudgetStatusResponse {
  const limit = 100000
  const used = Math.round((limit * usage_percent) / 100)
  return {
    scope: 'global',
    limit,
    used,
    remaining: limit - used,
    usage_percent,
    alert_level,
    estimated_cost: 1.5,
  }
}

function resetNotificationState(): void {
  resetDismissedNotificationIdsForTest()
  useNotificationStore.setState({
    notifications: [],
    isPanelOpen: false,
    activeBlockingNotification: null,
  })
}

// ────────────────────────── 合并映射（告警 → 通知条目载荷） ──────────────────────────

describe('layoutAlertToNotificationInput — 告警项映射为通知条目（错误样式可辨别）', () => {
  it('error tone → critical 优先级 + error 分类（红色条目面）+ 动作按钮带 payload', () => {
    const item = makeAlert({
      message: '内核连接失败，请检查内核是否运行',
      actionLabel: '查看监控',
      action: 'open-page',
      pageRef: PLUGIN_DECLARED_PAGES.monitoring.monitoring,
    })
    const input = layoutAlertToNotificationInput(item)

    expect(input.priority).toBe('critical')
    expect(input.category).toBe('error')
    expect(input.title).toBe('内核连接失败，请检查内核是否运行')
    expect(input.isBlocking).toBe(false)
    expect(input.actions).toHaveLength(1)
    expect(input.actions![0].label).toBe('查看监控')
    expect(input.actions![0].action).toBe('custom')
    expect(input.actions![0].payload).toEqual({
      layoutAlert: {
        action: 'open-page',
        pageRef: PLUGIN_DECLARED_PAGES.monitoring.monitoring,
      },
    })
  })

  it('warning tone → high 优先级 + alert 分类（警示面）；无动作项无按钮', () => {
    const input = layoutAlertToNotificationInput(
      makeAlert({ tone: 'warning', message: '连接断开，正在重连（第 2 次）…' }),
    )
    expect(input.priority).toBe('high')
    expect(input.category).toBe('alert')
    expect(input.actions).toBeUndefined()

    const approval = layoutAlertToNotificationInput(
      makeAlert({
        id: 'approval',
        kind: 'approval',
        tone: 'warning',
        message: '有 2 项审批待处理',
        actionLabel: '去处理',
      }),
    )
    // 有 actionLabel 无 action 声明 → 按钮在，payload 动作缺省（执行器零动作）
    expect(approval.actions![0].label).toBe('去处理')
    expect(approval.actions![0].payload).toEqual({ layoutAlert: { action: undefined, pageRef: undefined } })
  })
})

// ────────────────────────── 动作执行器（通知条目按钮落点） ──────────────────────────

describe('runLayoutAlertAction — 告警动作执行', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('reconnect → globalWS.forceReconnect（一键重连，不打开监控面板）', async () => {
    const globalWsMod = await import('@/services/websocket/GlobalWebSocket')
    const forceSpy = vi.spyOn(globalWsMod.globalWS, 'forceReconnect').mockImplementation(() => {})
    const openSpy = vi.mocked(openDeclaredPluginPage)

    runLayoutAlertAction({ action: 'reconnect' })

    expect(forceSpy).toHaveBeenCalledTimes(1)
    expect(openSpy).not.toHaveBeenCalled()
  })

  it('open-page → openDeclaredPluginPage（声明引用直达监控页）；无动作 payload 零动作', async () => {
    const globalWsMod = await import('@/services/websocket/GlobalWebSocket')
    const forceSpy = vi.spyOn(globalWsMod.globalWS, 'forceReconnect').mockImplementation(() => {})
    const openSpy = vi.mocked(openDeclaredPluginPage)

    runLayoutAlertAction({ action: 'open-page', pageRef: PLUGIN_DECLARED_PAGES.monitoring.monitoring })
    expect(openSpy).toHaveBeenCalledWith(PLUGIN_DECLARED_PAGES.monitoring.monitoring)
    expect(forceSpy).not.toHaveBeenCalled()

    openSpy.mockClear()
    runLayoutAlertAction({})
    expect(openSpy).not.toHaveBeenCalled()
    expect(forceSpy).not.toHaveBeenCalled()
  })
})

// ────────────────────────── 条件同步（告警 → 通知中心条目） ──────────────────────────

describe('useLayoutAlertNotifications — 系统异常同步为通知中心条目', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    resetNotificationState()
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED },
      pendingInteractions: [],
    })
  })
  afterEach(() => {
    vi.useRealTimers()
    mockBudget.current = null
  })

  it('断开 → 通知中心出现 connection 条目（红色错误样式 + 查看监控按钮）', async () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 3 },
    })
    renderHook(() => useLayoutAlertNotifications())

    // 同步引擎 effect 挂载即落库（act 内 flush）
    const entry = useNotificationStore.getState().notifications[0]
    expect(entry).toBeTruthy()
    expect(entry.id).toBe('connection#1')
    expect(entry.title).toBe('内核连接已断开，正在尝试恢复…')
    expect(entry.priority).toBe('critical')
    expect(entry.category).toBe('error')
    expect(entry.actions![0].label).toBe('查看监控')
  })

  it('解除 → 条目保留 4s 后自动移除；保留期内复燃沿用同一条目（不双条）', async () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 3 },
    })
    renderHook(() => useLayoutAlertNotifications())
    expect(useNotificationStore.getState().notifications).toHaveLength(1)

    // 解除：条目仍在（保留期）
    act(() => {
      useLayoutModeStore.setState({ connectionStatus: { ...CONNECTED } })
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(1)

    // 保留期内复燃（连接失败态）→ 同一条目更新，不新增
    act(() => {
      useLayoutModeStore.setState({
        connectionStatus: { ...CONNECTED, state: 'failed', reconnectAttempt: 0 },
      })
    })
    const revived = useNotificationStore.getState().notifications
    expect(revived).toHaveLength(1)
    expect(revived[0].id).toBe('connection#1')
    expect(revived[0].title).toBe('内核连接失败，请检查内核是否运行')

    // 解除且保留期走完 → 移除
    act(() => {
      useLayoutModeStore.setState({ connectionStatus: { ...CONNECTED } })
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })

  it('用户关闭后同轮不回弹；恢复后再触发以新 episode 重报', async () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 1 },
    })
    renderHook(() => useLayoutAlertNotifications())
    expect(useNotificationStore.getState().notifications).toHaveLength(1)

    // 用户关闭（同轮：条件仍活跃，重连计数变化不得回弹）
    act(() => {
      useNotificationStore.getState().dismissNotification('connection#1')
    })
    act(() => {
      useLayoutModeStore.setState({
        connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 2 },
      })
    })
    expect(useNotificationStore.getState().notifications).toHaveLength(0)

    // 恢复（保留期走完）→ 再断开 → 新 episode 重报
    act(() => {
      useLayoutModeStore.setState({ connectionStatus: { ...CONNECTED } })
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4000)
    })
    act(() => {
      useLayoutModeStore.setState({
        connectionStatus: { ...CONNECTED, state: 'failed', reconnectAttempt: 0 },
      })
    })
    const entries = useNotificationStore.getState().notifications
    expect(entries).toHaveLength(1)
    expect(entries[0].id).toBe('connection#2')
  })

  it('审批待处理 → approval 条目（警示样式，计数随 pending 变化更新）', () => {
    useLayoutModeStore.setState({
      pendingInteractions: [
        { id: 'i1', executionId: 'e1', prompt: '允许执行命令', timestamp: '2026-01-01T00:00:00Z' },
      ],
    })
    renderHook(() => useLayoutAlertNotifications())
    expect(useNotificationStore.getState().notifications[0]).toMatchObject({
      id: 'approval#1',
      priority: 'high',
      category: 'alert',
      title: '有 1 项审批待处理',
    })

    act(() => {
      useLayoutModeStore.setState({
        pendingInteractions: [
          { id: 'i1', executionId: 'e1', prompt: '允许执行命令', timestamp: '2026-01-01T00:00:00Z' },
          { id: 'i2', executionId: 'e2', prompt: '允许写文件', timestamp: '2026-01-01T00:00:00Z' },
        ],
      })
    })
    expect(useNotificationStore.getState().notifications[0].title).toBe('有 2 项审批待处理')
  })

  it('budget：alert_level=warning 过阈 → 通知条目出现（含查看成本按钮）', async () => {
    mockBudget.current = makeBudget('warning', 85)
    renderHook(() => useLayoutAlertNotifications())
    await act(async () => {}) // flush getBudgetStatus promise → 条件入列

    const entry = useNotificationStore.getState().notifications.find((n) => n.id === 'budget#1')
    expect(entry).toBeTruthy()
    expect(entry!.priority).toBe('high')
    expect(entry!.title).toContain('接近限额')
    expect(entry!.actions![0].label).toBe('查看成本')
  })
})

// ────────────────────────── useLayoutAlerts 派生口径（数据源，不变） ──────────────────────────

describe('useLayoutAlerts — 从 layoutModeStore 派生告警', () => {
  beforeEach(() => {
    mockBudget.current = null
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED },
      pendingInteractions: [],
    })
  })

  it('连接正常且无审批 → 空告警', () => {
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(0)
  })

  it('断开/失败 → connection 告警', () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 3 },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(1)
    expect(result.current[0].kind).toBe('connection')
    expect(result.current[0].tone).toBe('error')
    // E6 声明面化：断连告警导航动作=监控页声明引用（open-page + pageRef）
    expect(result.current[0].action).toBe('open-page')
    expect(result.current[0].pageRef).toEqual(
      expect.objectContaining({ pluginId: 'monitoring', pageId: 'monitoring' }),
    )
  })

  it('重连中（未达阈值）→ warning 横幅动态显示第 N 次，无按钮', () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'reconnecting', reconnectAttempt: 2 },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(1)
    expect(result.current[0].kind).toBe('connection')
    expect(result.current[0].tone).toBe('warning')
    expect(result.current[0].message).toContain('正在重连')
    expect(result.current[0].message).toContain('第 2 次')
    expect(result.current[0].actionLabel).toBeUndefined()
  })

  it('连续失败达阈值 → 降级 error 横幅 + 立即重连动作（自动重连不停止的提示）', () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'reconnecting', reconnectAttempt: 12 },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(1)
    expect(result.current[0].tone).toBe('error')
    expect(result.current[0].message).toContain('多次重连失败')
    expect(result.current[0].actionLabel).toBe('立即重连')
    expect(result.current[0].action).toBe('reconnect')
  })

  it('重连次数恰达阈值等值（attempt === RECONNECT_DEGRADE_ATTEMPTS）→ 已降级 error（>= 边界）', () => {
    useLayoutModeStore.setState({
      connectionStatus: {
        ...CONNECTED,
        state: 'reconnecting',
        reconnectAttempt: RECONNECT_DEGRADE_ATTEMPTS,
      },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current).toHaveLength(1)
    expect(result.current[0].tone).toBe('error')
    expect(result.current[0].message).toContain('多次重连失败')
  })

  it('审批待处理 → approval 告警（含数量）', () => {
    useLayoutModeStore.setState({
      pendingInteractions: [
        {
          id: 'i1',
          executionId: 'e1',
          prompt: '允许执行命令',
          timestamp: '2026-01-01T00:00:00Z',
        },
        {
          id: 'i2',
          executionId: 'e2',
          prompt: '允许写文件',
          timestamp: '2026-01-01T00:00:00Z',
        },
      ],
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current[0].kind).toBe('approval')
    expect(result.current[0].message).toContain('2')
  })

  it('连接 + 审批同时异常 → 两条告警', () => {
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'failed' },
      pendingInteractions: [
        {
          id: 'i1',
          executionId: 'e1',
          prompt: 'p',
          timestamp: '2026-01-01T00:00:00Z',
        },
      ],
    })
    const { result } = renderHook(() => useLayoutAlerts())
    expect(result.current.map((a) => a.kind).sort()).toEqual(['approval', 'connection'])
  })
})

describe('useLayoutAlerts budget — cost_control 预算告警（治理债清理 7.5.3）', () => {
  beforeEach(() => {
    mockBudget.current = null
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED },
      pendingInteractions: [],
    })
  })
  afterEach(() => {
    mockBudget.current = null
  })

  it('alert_level=warning → budget 告警出现（warning tone，含使用率）', async () => {
    mockBudget.current = makeBudget('warning', 85.4)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {}) // flush getBudgetStatus promise → setState

    const budgetItems = result.current.filter((a) => a.kind === 'budget')
    expect(budgetItems).toHaveLength(1)
    expect(budgetItems[0].id).toBe('budget')
    expect(budgetItems[0].tone).toBe('warning')
    expect(budgetItems[0].message).toContain('85')
    expect(budgetItems[0].message).toContain('接近限额')
    expect(budgetItems[0].actionLabel).toBe('查看成本')
    // E6 声明面化：budget 告警导航动作显式声明（不再由消费方按 kind 硬路由）
    expect(budgetItems[0].action).toBe('open-page')
    expect(budgetItems[0].pageRef).toEqual(
      expect.objectContaining({ pluginId: 'monitoring', pageId: 'monitoring' }),
    )
  })

  it('alert_level=info → 不出现 budget 告警', async () => {
    mockBudget.current = makeBudget('info', 50)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current).toHaveLength(0)
  })

  it('alert_level=critical → error tone', async () => {
    mockBudget.current = makeBudget('critical', 92)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    const budgetItems = result.current.filter((a) => a.kind === 'budget')
    expect(budgetItems).toHaveLength(1)
    expect(budgetItems[0].tone).toBe('error')
    expect(budgetItems[0].message).toContain('92')
  })

  it('exhausted 覆盖 warning：cost_update 复查后只留一条 error', async () => {
    // 先 warning 出一条
    mockBudget.current = makeBudget('warning', 85)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})
    expect(result.current.filter((a) => a.kind === 'budget')).toHaveLength(1)
    expect(result.current.filter((a) => a.kind === 'budget')[0].tone).toBe('warning')

    // 预算耗尽 → cost_update 事件到达 → 复查 → 同 id 覆盖，仅一条 error
    mockBudget.current = makeBudget('exhausted', 101)
    act(() => {
      ;(globalWS as unknown as { _emit: (event: string, data: unknown) => void })._emit(
        WS_SERVER_EVENTS.COST_UPDATE,
        { type: 'cost_update' },
      )
    })
    await act(async () => {})

    const budgetItems = result.current.filter((a) => a.kind === 'budget')
    expect(budgetItems).toHaveLength(1)
    expect(budgetItems[0].tone).toBe('error')
    expect(budgetItems[0].id).toBe('budget')
    expect(budgetItems[0].message).toContain('预算已耗尽')
  })

  it('exhausted 单值起始 → 仅一条 budget 告警（不 warning+error 双条）', async () => {
    mockBudget.current = makeBudget('exhausted', 100)
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current).toHaveLength(1)
    expect(result.current[0].kind).toBe('budget')
    expect(result.current[0].tone).toBe('error')
  })

  it('预算状态获取失败/无数据 → 不出现 budget 告警（fail-open 不打扰）', async () => {
    mockBudget.current = null
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current).toHaveLength(0)
  })

  it('budget 与 connection 并存 → 两条告警', async () => {
    mockBudget.current = makeBudget('warning', 85)
    useLayoutModeStore.setState({
      connectionStatus: { ...CONNECTED, state: 'disconnected', reconnectAttempt: 2 },
    })
    const { result } = renderHook(() => useLayoutAlerts())
    await act(async () => {})

    expect(result.current.map((a) => a.kind).sort()).toEqual(['budget', 'connection'])
  })
})
