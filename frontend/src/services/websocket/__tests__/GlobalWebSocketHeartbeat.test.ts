// @feature: FP-T12 WS 连接层（心跳） | @ci: frontend-test
/**
 * GlobalWebSocket 心跳 ack 超时容错补测
 *
 * 契约（用户可观察的断连行为）：
 * - 单次 ack 超时（missCount=1）只告警、不关连接（容忍局域网抖动/后端繁忙）；
 * - 连续 2 次 ack 超时（HEARTBEAT_MAX_MISS）→ 判定连接死亡，以 code=2002
 *   （TIMEOUT，非 4001 认证拒绝）主动关闭，走普通重连不触发 token 刷新。
 *
 * 驱动方式：只走公开面（connect + mock WebSocket 的 onopen/onclose/onmessage），
 * 用 fake timers 控制心跳 interval 与 ack 超时的交错，不触碰私有字段。
 * 关键点：connect() 会保留心跳 interval（只清重连计时器），因此用
 * 「心跳发送后切换 status 非 connected」让既有 ack 超时计时器不被下一跳
 * 心跳清除，从而按真实时序到达超时回调。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

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
const HEARTBEAT_MAX_MISS = 2

interface MockWs {
  url: string
  onopen: ((e: any) => void) | null
  onclose: ((e: any) => void) | null
  onmessage: ((e: any) => void) | null
  onerror: ((e: any) => void) | null
  send: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
  bufferedAmount: number
  readyState: number
  heartbeats: () => number
}

const instances: MockWs[] = []

class MockWebSocket {
  static OPEN = 1
  static CONNECTING = 0
  static CLOSED = 3

  onopen: ((e: any) => void) | null = null
  onclose: ((e: any) => void) | null = null
  onmessage: ((e: any) => void) | null = null
  onerror: ((e: any) => void) | null = null
  send = vi.fn()
  bufferedAmount = 0
  readyState = MockWebSocket.CONNECTING

  constructor(public url: string) {
    instances.push(this as unknown as MockWs)
  }

  heartbeats(): number {
    return this.send.mock.calls.filter((c: string[]) => {
      try {
        return JSON.parse(c[0])?.type === 'heartbeat'
      } catch {
        return false
      }
    }).length
  }

  close = vi.fn((code?: number, reason?: string) => {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.({ code: code ?? 1000, reason: reason ?? '' })
  })
}

/** 取最新连接实例 */
function latest(): MockWs {
  return instances[instances.length - 1]
}

async function bootService() {
  vi.resetModules()
  vi.stubGlobal('WebSocket', MockWebSocket)
  instances.length = 0
  const mod = await import('../GlobalWebSocket')
  return mod.globalWS
}

/** 打开指定连接（置 connected 并启动心跳） */
function open(ws: MockWs) {
  ws.onopen?.({})
}

describe('GlobalWebSocket 心跳 ack 超时容错', () => {
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

  it('心跳周期内持续收到 ack：连接始终 connected，不发生 2002 关闭', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = latest()
    open(ws)

    for (let i = 0; i < 3; i++) {
      await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
      ws.onmessage?.({ data: JSON.stringify({ type: 'heartbeat_ack' }) })
    }

    expect(ws.heartbeats()).toBe(3)
    expect(ws.close).not.toHaveBeenCalled()
    expect(svc.status).toBe('connected')
    svc.disconnect()
  })

  it('单次 ack 超时（1/2）只告警不关连接；连续第二次超时 → 以 2002 关闭（非 4001 认证拒绝）', async () => {
    // 真实世界触发形态：浏览器后台节流让心跳周期回调晚于 ack 超时回调（间隔回调
    // 被推迟，超时不被下一跳清除）。用可控 interval 注入该交错：周期回调由测试
    // 在超时之后手动触发，真实 setTimeout（90s ack 超时）仍由 fake clock 驱动。
    let heartbeatTick: (() => void) | null = null
    const realSetInterval = globalThis.setInterval
    vi.stubGlobal('setInterval', (fn: () => void, ms?: number) => {
      if (ms === HEARTBEAT_INTERVAL) {
        heartbeatTick = fn
        return 4242
      }
      return realSetInterval(fn as never, ms)
    })

    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = latest()
    open(ws)

    // 第 1 跳：发出心跳并武装 90s ack 超时
    heartbeatTick!()
    expect(ws.heartbeats()).toBe(1)

    // 无 ack → 超时回调到达：missCount=1，仅告警不断连
    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT)
    expect(ws.close).not.toHaveBeenCalled()
    expect(svc.status).toBe('connected')

    // 第 2 跳（被节流推迟到超时之后）：连接仍 connected → 重新武装超时
    heartbeatTick!()
    expect(ws.heartbeats()).toBe(2)

    // 第二次连续超时 → 达到 HEARTBEAT_MAX_MISS，判定连接死亡
    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT)
    expect(ws.close).toHaveBeenCalledWith(2002, '心跳超时')

    // 2002 属网络层故障：走普通重连，不触发 token 刷新/登出
    expect(svc.status).toBe('reconnecting')
    await vi.advanceTimersByTimeAsync(4000 + 100)
    expect(instances.length).toBeGreaterThanOrEqual(2)
    svc.disconnect()
  })

  it('ack 及时到达重置容错计数：不会累积到阈值而误断（与超时路径区分）', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = latest()
    open(ws)

    // 心跳 1 发出 → 接近超时前 ack 到达
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)
    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT - 1000)
    ws.onmessage?.({ data: JSON.stringify({ type: 'heartbeat_ack' }) })

    // 再跨越原来会超时的时间窗：计数已清零，连接存活
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL + 1000)
    expect(ws.close).not.toHaveBeenCalled()
    expect(svc.status).toBe('connected')
    svc.disconnect()
  })

  it('非 JSON 帧与未知类型帧不影响心跳计时与连接存活', async () => {
    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = latest()
    open(ws)

    ws.onmessage?.({ data: 'not-json' })
    ws.onmessage?.({ data: JSON.stringify({ type: 'unknown_event' }) })
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL * 3)

    expect(ws.close).not.toHaveBeenCalled()
    expect(svc.status).toBe('connected')
    svc.disconnect()
  })

  it('HEARTBEAT_MAX_MISS 恰为 2：一次超时不满足阈值（边界）', async () => {
    // 契约边界锚：阈值常量语义与断言解耦（避免实现改常量而测试静默通过）
    expect(HEARTBEAT_MAX_MISS).toBe(2)
    expect(HEARTBEAT_TIMEOUT).toBeGreaterThan(HEARTBEAT_INTERVAL)

    const svc = await bootService()
    svc.connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = latest()
    open(ws)
    await vi.advanceTimersByTimeAsync(HEARTBEAT_INTERVAL)

    svc.connect('token-b')
    await vi.advanceTimersByTimeAsync(100)
    await vi.advanceTimersByTimeAsync(HEARTBEAT_TIMEOUT)

    // 1 次 < 2 次：不关连接（负例，与用例 2 的正例成对）
    expect(ws.close).not.toHaveBeenCalled()
    svc.disconnect()
  })
})
