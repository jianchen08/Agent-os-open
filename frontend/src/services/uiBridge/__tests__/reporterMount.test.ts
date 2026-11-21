/** @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1生产接线 | @vision V2 全能闭环 | @ci frontend-test */
/**
 * uiBridge reporter 生产挂载入口验收测试（AC-P1-7，TDD Red——API 契约由本文件定义）
 *
 * 需求真源（只读，冲突回报调度方）：
 * - docs/project/侧边栏UI对象化/需求冻结_2026-11-18.md §2.1（client_id=localStorage
 *   稳定生成）+ §2.3（send=唯一外发点）/§三 AC-P1-7
 * - ADR 2026-11-18-sidebar-ui-object-model（S1「纯函数+薄接线」：本件测接线与
 *   生命周期，纯函数面（builder/scheduler）归 P0 签发件 stateReporter.test.ts，
 *   两文件零重叠）
 *
 * 被测契约 = ../reporterMount.ts（实现方按本契约交付，模块当前不存在 →
 * 整文件 import 失败属预期 Red）：
 *
 *   export interface UiBridgeReporterHandle {
 *     // 本端稳定 client_id（localStorage 键 'ui_bridge_client_id'，非空 ≤64 字符）
 *     getClientId(): string
 *     // 拆除：停调度器 + 退订全部 store/WS 订阅，无残留定时器（不泄漏）
 *     dispose(): void
 *   }
 *   export interface ReporterMountOptions {
 *     // 上报外发注入点（唯一 HTTP mock 点）；缺省 = 真实 POST /ext/ui_bridge/state
 *     //（frontend/src/services/api 既有客户端，Authorization 内积惯例）
 *     send?: (report: object) => Promise<void>
 *     // 注入时钟（透传 createReportScheduler §2.3 契约位）
 *     now?: () => number
 *     debounceMs?: number
 *     heartbeatMs?: number
 *   }
 *   export function mountUiBridgeReporter(opts?: ReporterMountOptions): UiBridgeReporterHandle
 *
 * 行为锚（AC-P1-7 ①~⑤ 逐条）：
 * ① client_id 经 localStorage 键 'ui_bridge_client_id' 稳定生成（首生成后恒定、
 *    跨重挂（=刷新模拟）不变；已有值复用不重生）；
 * ② 挂载即首次上报（不等防抖窗），缺省闭包 = 真实 POST /ext/ui_bridge/state；
 * ③ WS 重连成功本地事件（GlobalWebSocket 'reconnected'，WS_LOCAL_EVENTS.RECONNECTED）
 *    → 调度器 onReconnect（立即上报）；
 * ④ 订阅面四源——uiStore（sidebarCollapsed/sidebarActiveView）、notificationStore
 *    未读数、layoutModeStore 活动页签、会话列表缓存（TanStack Query queryKeys.sessions），
 *    变更触发防抖上报；
 * ⑤ dispose 后无残留定时器与订阅（vi.getTimerCount 回落基线 + 行为零上报）。
 *
 * mock 仅限注入点：WS（globalWS 模块 mock）、HTTP（send 注入 / apiClient 模块
 * mock）；四源 store 走真实 zustand/query 实例（断行为不断实现）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useUIStore } from '@/stores/uiStore'
import { mountUiBridgeReporter, type UiBridgeReporterHandle } from '../reporterMount'
import type { Session } from '@/types/models'

// ── WS 外部依赖 mock：捕获订阅/退订，测试内定向发 reconnected 本地事件 ──

const wsSubscribeMock = vi.fn()
const wsUnsubscribeMock = vi.fn()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, handler: (data: unknown) => void) => {
      wsSubscribeMock(event, handler)
    },
    unsubscribe: (event: string, handler: (data: unknown) => void) => {
      wsUnsubscribeMock(event, handler)
    },
    status: 'connected',
  },
}))

const handlersFor = (event: string): Array<(data: unknown) => void> =>
  wsSubscribeMock.mock.calls
    .filter(([name]) => name === event)
    .map(([, handler]) => handler as (data: unknown) => void)

const emitReconnected = (): void => {
  for (const handler of handlersFor('reconnected')) handler({ status: 'connected' })
}

// ── HTTP 外部依赖 mock：缺省 send 闭包 = 既有 api 客户端 ────────────────

const apiPostMock = vi.fn()

vi.mock('@/services/api/client', () => ({
  default: { post: (...args: unknown[]) => apiPostMock(...args) },
  apiClient: { post: (...args: unknown[]) => apiPostMock(...args) },
}))

// ── 用例 ──────────────────────────────────────────────────────────────

const CLIENT_ID_KEY = 'ui_bridge_client_id'
const CLIENT_ID_BOUND = 64

const handles: UiBridgeReporterHandle[] = []
const mount = (opts?: Parameters<typeof mountUiBridgeReporter>[0]): UiBridgeReporterHandle => {
  const handle = mountUiBridgeReporter(opts)
  handles.push(handle)
  return handle
}

const makeSession = (id: string, title: string): Session =>
  ({
    id,
    title,
    createdAt: '2026-11-18T00:00:00Z',
    updatedAt: '2026-11-18T00:00:00Z',
    messageCount: 0,
  }) as Session

const seedSessionCache = (): void => {
  queryClient.setQueryData<Session[]>(queryKeys.sessions, [makeSession('sess-seed', '种子会话')])
}

const seedWorkspaceTab = (): string => {
  useLayoutModeStore.getState().addWorkspaceTab({
    id: 'tab-under-test',
    title: '测试页签',
    moduleId: '__panel_under_test__',
    component: 'pipeline_manager',
    isActive: true,
    isPinned: false,
  })
  return 'tab-under-test'
}

beforeEach(() => {
  vi.useFakeTimers()
  wsSubscribeMock.mockClear()
  wsUnsubscribeMock.mockClear()
  apiPostMock.mockClear()
  apiPostMock.mockResolvedValue({})
  localStorage.clear()
  seedSessionCache()
})

afterEach(() => {
  for (const handle of handles.splice(0)) handle.dispose()
  queryClient.clear()
  useNotificationStore.setState({ notifications: [], readIds: new Set<string>() })
  useUIStore.setState({ sidebarCollapsed: false, sidebarActiveView: 'sessions' })
  localStorage.clear()
  vi.useRealTimers()
})

// ── ① client_id 经 localStorage 稳定生成 ──────────────────────────────

describe('client_id 稳定生成（AC-P1-7 ①）', () => {
  it('首生成落 localStorage 键 ui_bridge_client_id，跨重挂（刷新模拟）恒定，非空且 ≤64 字符', () => {
    const first = mount({ send: vi.fn().mockResolvedValue(undefined) })
    const id1 = first.getClientId()
    expect(id1).toBeTruthy()
    expect(id1.length).toBeLessThanOrEqual(CLIENT_ID_BOUND)
    expect(localStorage.getItem(CLIENT_ID_KEY)).toBe(id1)

    first.dispose()
    const second = mount({ send: vi.fn().mockResolvedValue(undefined) })
    // 重新挂载（=刷新后新模块实例）读到同一 client_id
    expect(second.getClientId()).toBe(id1)
    second.dispose()
  })

  it('localStorage 已有值：复用不重生（含 CJK 场景）', () => {
    localStorage.setItem(CLIENT_ID_KEY, 'fe-预置-✨')
    const handle = mount({ send: vi.fn().mockResolvedValue(undefined) })
    expect(handle.getClientId()).toBe('fe-预置-✨')
  })
})

// ── ② 挂载即首次上报 + 缺省闭包 = 真实 POST ───────────────────────────

describe('首次上报（AC-P1-7 ②）', () => {
  it('挂载即上报一次（不等防抖窗）：载荷携 client_id（=getClientId）与 region=sidebar', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const handle = mount({ send })

    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(1)
    const report = send.mock.calls[0][0] as Record<string, unknown>
    expect(report['client_id']).toBe(handle.getClientId())
    expect(report['region']).toBe('sidebar')
  })

  it('缺省 send 闭包 = 既有 api 客户端 POST /ext/ui_bridge/state', async () => {
    mount() // 不注入 send → 真实外发面（apiClient mock 承接）

    await vi.advanceTimersByTimeAsync(0)
    expect(apiPostMock).toHaveBeenCalledTimes(1)
    const [url, body] = apiPostMock.mock.calls[0] as [string, Record<string, unknown>]
    expect(url).toBe('/ext/ui_bridge/state')
    expect(typeof body['client_id']).toBe('string')
    expect(body['region']).toBe('sidebar')
  })
})

// ── ④ 订阅面四源：变更触发防抖上报 ────────────────────────────────────

describe('订阅面四源变更 → 防抖上报（AC-P1-7 ④）', () => {
  it('uiStore：sidebarCollapsed 变更 → 防抖窗口闭合后上报一次（499ms 未发 / 500ms 发）', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(1) // 挂载首发

    useUIStore.getState().setSidebarCollapsed(true)
    await vi.advanceTimersByTimeAsync(499)
    expect(send).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('uiStore：sidebarActiveView 变更同样触发上报（快照源第二字段）', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)

    useUIStore.getState().setSidebarActiveView('tasks')
    await vi.advanceTimersByTimeAsync(500)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('notificationStore：未读数变化（入列通知）→ 防抖上报', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)

    useNotificationStore.getState().addNotification({
      title: '测试通知',
      message: '未读数 +1',
      priority: 'low',
      category: 'info',
      isBlocking: false,
    })
    await vi.advanceTimersByTimeAsync(500)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('layoutModeStore：活动页签变更（addWorkspaceTab + setActiveTab）→ 防抖上报', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)
    const tabId = seedWorkspaceTab()

    useLayoutModeStore.getState().setActiveTab(tabId)
    await vi.advanceTimersByTimeAsync(500)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('会话列表缓存：queryKeys.sessions 缓存写入 → 防抖上报', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)

    queryClient.setQueryData<Session[]>(queryKeys.sessions, [
      makeSession('sess-seed', '种子会话'),
      makeSession('sess-new', '新会话'),
    ])
    await vi.advanceTimersByTimeAsync(500)
    expect(send).toHaveBeenCalledTimes(2)
  })

  it('防抖合并：窗口内 uiStore + notificationStore 两源先后变更 → 只上报一次', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)

    useUIStore.getState().setSidebarCollapsed(true)
    useNotificationStore.getState().addNotification({
      title: '第二源',
      message: '同窗合并',
      priority: 'low',
      category: 'info',
      isBlocking: false,
    })
    await vi.advanceTimersByTimeAsync(499)
    expect(send).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(send).toHaveBeenCalledTimes(2)
  })
})

// ── ③ WS 重连成功事件 → 立即上报 ─────────────────────────────────────

describe('reconnected 事件 → onReconnect（AC-P1-7 ③）', () => {
  it("订阅 'reconnected' 本地事件；事件到达立即上报（不等防抖窗）", async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    mount({ send })
    await vi.advanceTimersByTimeAsync(0)
    expect(handlersFor('reconnected').length).toBeGreaterThanOrEqual(1)

    emitReconnected()
    // 重连即发，同步可见、零定时器推进
    expect(send).toHaveBeenCalledTimes(2)
  })
})

// ── ⑤ dispose 后无残留定时器与订阅（不泄漏）──────────────────────────

describe('dispose 生命周期（AC-P1-7 ⑤）', () => {
  it('dispose 后定时器数回落挂载前基线（心跳/防抖无残留）', () => {
    const baseline = vi.getTimerCount()
    const handle = mount({ send: vi.fn().mockResolvedValue(undefined) })
    // 挂载后有心跳定时器在册
    expect(vi.getTimerCount()).toBeGreaterThan(baseline)

    handle.dispose()
    expect(vi.getTimerCount()).toBe(baseline)
  })

  it('dispose 后 store 变更与 reconnected 事件均零上报（订阅无残留）', async () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const handle = mount({ send })
    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(1)

    handle.dispose()

    useUIStore.getState().setSidebarCollapsed(true)
    queryClient.setQueryData<Session[]>(queryKeys.sessions, [makeSession('sess-after', '拆后写')])
    emitReconnected()
    await vi.advanceTimersByTimeAsync(1000)
    // dispose 后任何触发源不再上报
    expect(send).toHaveBeenCalledTimes(1)
  })

  it("dispose 后 reconnected 订阅已退订（unsubscribe 携原 handler）", () => {
    const send = vi.fn().mockResolvedValue(undefined)
    const handle = mount({ send })
    const subscribed = handlersFor('reconnected')
    expect(subscribed.length).toBeGreaterThanOrEqual(1)

    handle.dispose()
    expect(wsUnsubscribeMock).toHaveBeenCalledWith('reconnected', subscribed[0])
  })
})
