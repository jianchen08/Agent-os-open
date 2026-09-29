// @feature: FP-T12 WS 连接层（心跳） | @ci: frontend-test
/**
 * GlobalWebSocket 心跳判死（pong 新鲜度）测试
 *
 * 契约（用户可观察的断连行为）：
 * - 判死 = pong 新鲜度：距最近一次心跳 ack 超过 HEARTBEAT_TIMEOUT（90s，连续
 *   3 个 30s 周期）→ 以 code=2002（TIMEOUT，非 4001 认证拒绝）主动关闭，经
 *   onclose 走普通重连、不触发 token 刷新；
 * - 判死截止期只由 ack 到达刷新，tick 只做检查、不在 tick 上清掉重挂——每
 *   tick 重挂同一截止期会让超时回调永远活不过下一跳（判死成死代码，僵尸
 *   连接上状态恒 connected、重连不启，装机"内核未连接"横幅无法自愈）；
 * - 阈值内 ack 缺席（<3 周期）容忍不断连；非 pong 帧流量不刷新新鲜度；
 * - 判死一次性：close(2002) → onclose 之间（真实浏览器为异步窗口）不重复判死。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { finishWsServiceSetup, instances, type MockWebSocketInstance } from './helpers/mockWebSocket'

const { mockFetchWsTicket, mockUpdateConnectionStatus } = vi.hoisted(() => ({
  mockFetchWsTicket: vi.fn(),
  mockUpdateConnectionStatus: vi.fn(),
}))

vi.mock('@/services/auth/wsTicket', () => ({ fetchWsTicket: mockFetchWsTicket }))
vi.mock('@/services/auth/tokenLifecycle', () => ({
  isExpired: () => false,
  refresh: vi.fn().mockResolvedValue(undefined),
  getAccessToken: () => 'token-a',
  isAuthFailureFromError: () => false,
}))
vi.mock('@/services/authCallbacks', () => ({ triggerAuthExpired: vi.fn() }))
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: { getState: () => ({ updateConnectionStatus: mockUpdateConnectionStatus }) },
}))
vi.mock('@/utils/logger', () => ({
  loggers: { websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

const HEARTBEAT_INTERVAL = 30_000
const HEARTBEAT_TIMEOUT = 90_000

async function bootService() {
  vi.resetModules()
  const { service } = await finishWsServiceSetup()
  return service
}

/** 打开指定连接（置 connected 并启动心跳） */
function open(ws: MockWebSocketInstance) {
  ws.onopen?.({})
}

/** 统计已发送的 heartbeat 帧数 */
function heartbeatsOf(ws: MockWebSocketInstance): number {
  return ws.send.mock.calls.filter((c: string[]) => {
    try {
      return JSON.parse(c[0])?.type === 'heartbeat'
    } catch {
      return false
    }
  }).length
}

/** 模拟服务端心跳 ack（pong） */
function ack(ws: MockWebSocketInstance) {
  ws.onmessage?.({ data: JSON.stringify({ type: 'heartbeat_ack' }) })
}

describe('GlobalWebSocket 心跳判死（pong 新鲜度）', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    instances.length = 0
    mockFetchWsTicket.mockReset()
    mockFetchWsTicket.mockResolvedValue('ticket-hb')
    mockUpdateConnectionStatus.mockReset()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('pong 正常（每周期 ack 到达）：推进多倍判死阈值仍不关连', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = instances[instances.length - 1]
    open(ws)

    // 6 个周期 = 180s ≈ 2 倍判死阈值，每周期 ack 及时到达：截止期被持续刷新
    for (let i = 0; i < 6; i++) {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
      ack(ws)
    }

    expect(heartbeatsOf(ws)).toBe(6)
    expect(ws.close).not.toHaveBeenCalled()
    expect(svc.status).toBe('connected')
    svc.disconnect()
  })

  it('服务端停止回 pong：连续 3 个周期无 ack → close(2002)，重连路径启动', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = instances[instances.length - 1]
    open(ws)

    // 前两个周期（60s）无 ack：在 90s 容错窗口内，只发心跳不断连（边界：未达阈值）
    for (let i = 0; i < 2; i++) {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    }
    expect(ws.close).not.toHaveBeenCalled()
    expect(heartbeatsOf(ws)).toBe(2)

    // 第 3 个周期：距建连 90s 无 pong → 判死，code=2002（TIMEOUT，非 4001）
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    expect(ws.close).toHaveBeenCalledWith(2002, '心跳超时')
    expect(svc.status).toBe('reconnecting')

    // 走既有重连路径：退避 4s 后重新取票建连（不触发 token 刷新）
    await vi.advanceTimersByTimeAsync(4000 + 100)
    expect(instances.length).toBeGreaterThanOrEqual(2)
    svc.disconnect()
  })

  it('判死幂等：close(2002) 已调、onclose 未达的窗口内不重复判死', async () => {
    // 真实浏览器 close → onclose 为异步：以不触发 onclose 的 close 模拟该窗口
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = instances[instances.length - 1]
    open(ws)
    ws.close = vi.fn()

    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT)
    expect(ws.close).toHaveBeenCalledTimes(1)
    expect(ws.close).toHaveBeenCalledWith(2002, '心跳超时')

    // 窗口内再跨一个心跳周期：不得二次判死（非幂等实现会在此再次 close）
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    expect(ws.close).toHaveBeenCalledTimes(1)

    // onclose 到达后恢复既有重连路径
    ws.onclose?.({ code: 2002, reason: '心跳超时' })
    expect(svc.status).toBe('reconnecting')
    await vi.advanceTimersByTimeAsync(4000 + 100)
    expect(instances.length).toBeGreaterThanOrEqual(2)
    svc.disconnect()
  })

  it('ack 及时到达刷新新鲜度：临近阈值的 ack 将判死截止期整体推迟', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = instances[instances.length - 1]
    open(ws)

    // t≈75s（阈值前 15s）ack 到达：截止期从建连时刻刷新到该 ack 时刻
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    await vi.advanceTimersByTimeAsync(45_000)
    ack(ws)

    // 原截止期（建连 +90s）早已过去：新鲜度已被刷新，连接存活
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    expect(ws.close).not.toHaveBeenCalled()
    expect(svc.status).toBe('connected')

    // 距最后一次 ack 90s 无 pong → 在周期 tick 上判死
    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT)
    expect(ws.close).toHaveBeenCalledWith(2002, '心跳超时')
    svc.disconnect()
  })

  it('非 pong 帧（非 JSON/未知类型）不刷新新鲜度：帧流量不能替代 ack', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = instances[instances.length - 1]
    open(ws)

    // 每周期都有下行帧流量，但都不是 heartbeat_ack：不得据此绕过判死
    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
      ws.onmessage?.({ data: 'not-json' })
      ws.onmessage?.({ data: JSON.stringify({ type: 'unknown_event' }) })
    }

    expect(ws.close).toHaveBeenCalledWith(2002, '心跳超时')
    svc.disconnect()
  })
})
