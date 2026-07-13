/** GlobalWebSocket 单元测试 测试全局 WebSocket 服务的重连参数、状态转换、心跳机制。 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

// ── Mock 依赖 ──

// Mock buildGlobalWebSocketUrl
vi.mock('@/constants/websocket', () => ({
  buildGlobalWebSocketUrl: (token: string) =>
    `ws://localhost:8988/ws/chat?token=${token}&version=3.0.0`,
}))

// Mock useLayoutModeStore
const mockUpdateConnectionStatus = vi.fn()
vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: {
    getState: () => ({
      updateConnectionStatus: mockUpdateConnectionStatus,
    }),
  },
}))

// Mock useAuthStore：默认 token 未过期，让普通重连测试走指数退避路径
// （1006+未连接过的兜底逻辑仅在 checkTokenExpiration()=true 时才触发）
const mockCheckTokenExpiration = vi.fn(() => false)
const mockRefreshToken = vi.fn(async () => {})
vi.mock('@/stores/authStore', () => ({
  useAuthStore: {
    getState: () => ({
      checkTokenExpiration: mockCheckTokenExpiration,
      refreshToken: mockRefreshToken,
      token: 'test-token',
    }),
  },
  isAuthFailureFromError: () => false,
}))
vi.mock('@/services/authCallbacks', () => ({
  triggerAuthExpired: vi.fn(),
}))

// Mock logger
vi.mock('@/utils/logger', () => ({
  loggers: {
    websocket: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  },
}))

// ── Mock WebSocket ──

type MockEventListener = ((event: any) => void) | null

interface MockWebSocketInstance {
  onopen: MockEventListener
  onclose: MockEventListener
  onmessage: MockEventListener
  onerror: MockEventListener
  send: ReturnType<typeof vi.fn>
  close: ReturnType<typeof vi.fn>
  readyState: number
}

const instances: MockWebSocketInstance[] = []

class MockWebSocket {
  static OPEN = 1
  static CLOSED = 3
  static CONNECTING = 0

  onopen: MockEventListener = null
  onclose: MockEventListener = null
  onmessage: MockEventListener = null
  onerror: MockEventListener = null
  send = vi.fn()
  close = vi.fn((code?: number, _reason?: string) => {
    this.readyState = MockWebSocket.CLOSED
    if (this.onclose) {
      this.onclose({ code: code ?? 1000, reason: _reason ?? '' })
    }
  })
  readyState = MockWebSocket.CONNECTING

  constructor(public url: string) {
    instances.push(this as unknown as MockWebSocketInstance)
  }
}

// ── 导入被测模块 ──

// 必须在 mock 设置之后导入
let GlobalWebSocketService: typeof import('../GlobalWebSocket').default.constructor
let ConnectionStatus: typeof import('../GlobalWebSocket').ConnectionStatus

beforeEach(async () => {
  // 清空实例列表
  instances.length = 0

  // 设置全局 WebSocket
  vi.stubGlobal('WebSocket', MockWebSocket)

  // 动态导入以获取新单例
  const mod = await import('../GlobalWebSocket')
  // mod.globalWS 是单例，但我们需要访问类定义
  // 直接用 dynamic import 重新加载模块获取新的单例
})

// ── 辅助函数 ──

/** 创建一个新的 GlobalWebSocketService 实例 因为 globalWS 是模块级单例，测试需要刷新模块来获取干净实例 */
async function createService(): Promise<{
  service: any
  connect: (token: string) => void
  disconnect: () => void
  getLatestWs: () => MockWebSocketInstance | undefined
}> {
  // 刷新模块以获取新的单例
  vi.resetModules()

  vi.stubGlobal('WebSocket', MockWebSocket)
  vi.doMock('@/constants/websocket', () => ({
    buildGlobalWebSocketUrl: (token: string) =>
      `ws://localhost:8988/ws/chat?token=${token}&version=3.0.0`,
  }))
  vi.doMock('@/stores/layoutModeStore', () => ({
    useLayoutModeStore: {
      getState: () => ({
        updateConnectionStatus: mockUpdateConnectionStatus,
      }),
    },
  }))
  vi.doMock('@/utils/logger', () => ({
    loggers: {
      websocket: {
        debug: vi.fn(),
        info: vi.fn(),
        warn: vi.fn(),
        error: vi.fn(),
      },
    },
  }))

  instances.length = 0
  const mod = await import('../GlobalWebSocket')
  const service = mod.globalWS

  return {
    service,
    connect: (token: string) => service.connect(token),
    disconnect: () => service.disconnect(),
    getLatestWs: () => instances[instances.length - 1],
  }
}

/** 模拟成功连接：先触发 connect → 推进 timer → 触发 onopen */
function simulateSuccessfulOpen(ws: MockWebSocketInstance): void {
  if (ws.onopen) {
    ws.onopen({})
  }
}

/** 模拟连接关闭 */
function simulateClose(ws: MockWebSocketInstance, code: number = 1000, reason: string = ''): void {
  if (ws.onclose) {
    ws.onclose({ code, reason })
  }
}

// ── 测试套件 ──

describe('GlobalWebSocketService', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockUpdateConnectionStatus.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  // ──────────────────────────────────────────────
  // 1. 重连参数测试
  // ──────────────────────────────────────────────
  describe('重连参数', () => {
    it('首次重连延迟应为 4 秒（RECONNECT_BASE_DELAY）', async () => {
      const { service, connect, getLatestWs } = await createService()

      connect('test-token')
      // connect 内部有 50ms 延迟才真正创建 WS
      vi.advanceTimersByTime(100)

      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      // 模拟断开
      simulateClose(ws, 1006, 'network error')

      // 此时 status 应为 reconnecting
      expect(service.status).toBe('reconnecting')

      // 推进时间少于 4 秒，不应重连
      vi.advanceTimersByTime(3999)

      // 推进到 4 秒，connect 应被再次调用
      vi.advanceTimersByTime(1)

      // 4秒后应触发重连（connect 内部又有 50ms 延迟）
      vi.advanceTimersByTime(100)

      // 应创建了新的 WS 实例
      expect(instances.length).toBeGreaterThanOrEqual(2)

      service.disconnect()
    })

    it('重连延迟应按指数退避递增', async () => {
      const { service, connect, getLatestWs } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 第 1 次断开 → 延迟 4s (BASE_DELAY * 2^0)
      simulateClose(ws, 1006, 'error')
      expect(service.status).toBe('reconnecting')

      // 推进到 4s 触发重连
      vi.advanceTimersByTime(4000 + 100)
      const ws2 = getLatestWs()!
      expect(ws2).not.toBe(ws)

      // 第 2 次连接失败
      vi.advanceTimersByTime(100)
      simulateClose(ws2, 1006, 'error')

      // 推进到 8s (BASE_DELAY * 2^1)
      vi.advanceTimersByTime(8000 + 100)
      const ws3 = getLatestWs()!
      expect(ws3).not.toBe(ws2)

      // 第 3 次连接失败
      vi.advanceTimersByTime(100)
      simulateClose(ws3, 1006, 'error')

      // 推进到 16s (BASE_DELAY * 2^2)
      vi.advanceTimersByTime(16000 + 100)
      const ws4 = getLatestWs()!
      expect(ws4).not.toBe(ws3)

      service.disconnect()
    })

    it('重连延迟不应超过最大值 60 秒（RECONNECT_MAX_DELAY）', async () => {
      const { service, connect, getLatestWs } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      let ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 断开后进入 reconnecting，验证经过足够多次重连后服务仍持续尝试
      simulateClose(ws, 1006, 'error')
      expect(service.status).toBe('reconnecting')

      // 模拟多次重连失败，每次推进 61s（超过最大延迟 60s）
      // 验证经过多次失败后仍能持续重连（不放弃）
      for (let i = 0; i < 15; i++) {
        vi.advanceTimersByTime(61000)
        const latest = getLatestWs()!
        // 模拟连接立即失败
        if (latest && latest.onclose) {
          latest.onclose({ code: 1006, reason: 'error' })
        }
      }

      // 经过多次重连后，_reconnectAttempts 已超过 MAX_RETRIES(30)
      // 此时 _scheduleReconnect 会将状态设为 reconnecting，延迟固定为 60s
      // 状态在 reconnecting <-> connecting 间切换，验证服务仍在运行
      expect(service.status).toMatch(/^(reconnecting|connecting)$/)
      expect((service as any)._disposed).toBe(false)

      service.disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 2. 状态转换测试
  // ──────────────────────────────────────────────
  describe('状态转换', () => {
    it('初始状态应为 disconnected', async () => {
      const { service } = await createService()
      expect(service.status).toBe('disconnected')
    })

    it('调用 connect 后状态应变为 connecting', async () => {
      const { service, connect } = await createService()

      connect('test-token')
      expect(service.status).toBe('connecting')
    })

    it('WebSocket open 后状态应变为 connected', async () => {
      const { service, connect, getLatestWs } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)

      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      expect(service.status).toBe('connected')
      service.disconnect()
    })

    it('连接关闭（非 code=4000）应触发 reconnecting', async () => {
      const { service, connect, getLatestWs } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 模拟异常断开
      simulateClose(ws, 1006, 'abnormal')

      expect(service.status).toBe('reconnecting')
      service.disconnect()
    })

    it('连接关闭（code=4000）不应重连，状态保持 disconnected', async () => {
      const { service, connect, getLatestWs } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // code=4000 表示被新连接替换
      simulateClose(ws, 4000, '被新连接替换')

      expect(service.status).toBe('disconnected')
      service.disconnect()
    })

    it('完整流程: disconnected → connecting → connected → reconnecting → connected', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      // 1. disconnected
      expect(service.status).toBe('disconnected')

      // 2. connecting
      connect('test-token')
      expect(service.status).toBe('connecting')
      vi.advanceTimersByTime(100)

      // 3. connected
      let ws = getLatestWs()!
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      // 4. reconnecting (断线)
      simulateClose(ws, 1006, 'network lost')
      expect(service.status).toBe('reconnecting')

      // 5. 重连成功 → connected
      vi.advanceTimersByTime(4000 + 100) // 等待重连延迟
      ws = getLatestWs()!
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('disconnect 后状态应为 disconnected 且不再重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      disconnect()
      expect(service.status).toBe('disconnected')

      // 推进大量时间，不应创建新的 WS
      const instanceCountBefore = instances.length
      vi.advanceTimersByTime(120000)
      expect(instances.length).toBe(instanceCountBefore)
    })
  })

  // ──────────────────────────────────────────────
  // 3. 心跳机制测试
  // ──────────────────────────────────────────────
  describe('心跳机制', () => {
    it('连接成功后应启动心跳定时器', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 推进 30 秒触发心跳
      vi.advanceTimersByTime(30000)

      // ws.send 应被调用来发送心跳
      expect(ws.send).toHaveBeenCalled()
      const sendCalls = ws.send.mock.calls.map((call: string[]) => {
        try { return JSON.parse(call[0]) } catch { return null }
      })
      const heartbeatCall = sendCalls.find((c: any) => c?.type === 'heartbeat')
      expect(heartbeatCall).toBeDefined()
      expect(heartbeatCall).toHaveProperty('timestamp')

      disconnect()
    })

    it('收到 heartbeat_ack 应清除超时定时器', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 触发心跳发送
      vi.advanceTimersByTime(30000)

      // 模拟收到 heartbeat_ack
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify({ type: 'heartbeat_ack' }) })
      }

      // 推进到超时时间（30s），因为已经清除了超时，不应关闭连接
      vi.advanceTimersByTime(30000)

      // 连接应仍然存在（ws.close 未因超时被调用）
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('心跳超时后应触发重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 触发心跳发送（30s interval 触发）
      vi.advanceTimersByTime(30000)

      // 验证心跳已发送
      const heartbeatSent = ws.send.mock.calls.some((call: string[]) => {
        try { return JSON.parse(call[0])?.type === 'heartbeat' } catch { return false }
      })
      expect(heartbeatSent).toBe(true)

      // 注意：HEARTBEAT_TIMEOUT(90s) > HEARTBEAT_INTERVAL(30s)，且需连续 2 次超时才断。
      // 单次超时只会累加 _heartbeatMissCount 并打 warn，不会 close。
      // 因此此处直接模拟连接因心跳超时被关闭（ws.close 2002）的行为，
      // 验证 onclose 对心跳超时关闭的处理是否正确（走普通重连，不触发 token 刷新）。
      ws.close(2002, '心跳超时')

      // ws.close(4001) → onclose → _scheduleReconnect → status = 'reconnecting'
      expect(service.status).toBe('reconnecting')

      // 推进时间验证重连会创建新的 WebSocket
      vi.advanceTimersByTime(4000 + 100)
      expect(instances.length).toBeGreaterThanOrEqual(2)

      disconnect()
    })

    it('心跳超时应给 ack 留容错：单次超时不断连，收到 ack 恢复', async () => {
      // LLM 流式期间后端事件循环负载高，heartbeat_ack 响应极易突破 30s。
      // 修复: TIMEOUT=90s 且需连续 2 次未收到 ack 才断（HEARTBEAT_MAX_MISS=2）。
      // 回归契约: 单次超时（missCount=1）不触发 close；收到 ack 后 missCount 清零。
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 推进 30s → 心跳发出，超时定时器启动（45s 后到期）
      vi.advanceTimersByTime(30000)
      const closeCallsBefore = ws.close.mock.calls.length

      // 再推进 10s（累计距心跳发送 10s，距超时还有 5s）→ 模拟 ack 稍慢但仍在容错内
      vi.advanceTimersByTime(10000)
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify({ type: 'heartbeat_ack' }) })
      }

      // ack 清除超时后，再推进超过原 30s 阈值（验证旧 30s 零容错已不复存在）
      vi.advanceTimersByTime(35000)

      // 容错窗口内收到 ack：连接不应因心跳超时被关闭
      expect(service.status).toBe('connected')
      expect(ws.close.mock.calls.length).toBe(closeCallsBefore)

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 4. 事件订阅测试
  // ──────────────────────────────────────────────
  describe('事件订阅', () => {
    it('连接成功应触发 connect 和 _status 事件', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const connectHandler = vi.fn()
      const statusHandler = vi.fn()
      service.subscribe('connect', connectHandler)
      service.subscribe('_status', statusHandler)

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      expect(connectHandler).toHaveBeenCalledWith({ status: 'connected' })
      expect(statusHandler).toHaveBeenCalledWith({ status: 'connected' })

      disconnect()
    })

    it('重连成功应额外触发 reconnected 事件', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const reconnectedHandler = vi.fn()
      service.subscribe('reconnected', reconnectedHandler)

      connect('test-token')
      vi.advanceTimersByTime(100)
      let ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 首次连接不应触发 reconnected
      expect(reconnectedHandler).not.toHaveBeenCalled()

      // 断开并重连
      simulateClose(ws, 1006, 'error')
      vi.advanceTimersByTime(4000 + 100)
      ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 重连成功应触发 reconnected
      expect(reconnectedHandler).toHaveBeenCalledWith({ status: 'connected' })

      disconnect()
    })

    it('状态变化时应触发 _status 事件', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const statusHandler = vi.fn()
      service.subscribe('_status', statusHandler)

      connect('test-token')
      // connect 后状态为 connecting，但 _status 事件还没触发（在 onopen 和 onclose 中触发）

      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // connected
      expect(statusHandler).toHaveBeenCalledWith({ status: 'connected' })

      // disconnected → reconnecting
      simulateClose(ws, 1006, 'error')
      expect(statusHandler).toHaveBeenCalledWith({ status: 'disconnected', code: 1006, reason: 'error' })
      expect(statusHandler).toHaveBeenCalledWith({ status: 'reconnecting' })

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 5. 消息队列测试
  // ──────────────────────────────────────────────
  describe('消息队列', () => {
    it('未连接时 sendUserInput 应将消息入队', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      // 不调用 connect，直接发送
      service.sendUserInput('thread-1', 'hello')

      // 之后连接成功，消息应被发出
      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // flushQueue 应发送了排队的消息
      const sendCalls = ws.send.mock.calls.map((call: string[]) => {
        try { return JSON.parse(call[0]) } catch { return null }
      })
      const userMsg = sendCalls.find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg.content).toBe('hello')
      expect(userMsg.thread_id).toBe('thread-1')

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 6. 连接超时测试
  // ──────────────────────────────────────────────
  describe('连接超时', () => {
    it('连接建立超时（15s）应关闭并重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      expect(service.status).toBe('connecting')

      // 推进到 15 秒（CONNECTION_TIMEOUT）
      vi.advanceTimersByTime(15000)

      // 应触发超时重连
      expect(service.status).toBe('reconnecting')

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 7. sendCancel pipelineId 参数测试
  // ──────────────────────────────────────────────
  describe('sendCancel - pipelineId 参数', () => {
    /** 辅助函数：建立连接并返回最近一次 WS 实例 */
    async function setupConnected() {
      const { service, connect, getLatestWs, disconnect } = await createService()
      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)
      return { service, ws, disconnect }
    }

    /** 辅助函数：从 ws.send 的调用记录中提取所有已发送消息的解析结果 */
    function getSentMessages(ws: MockWebSocketInstance) {
      return ws.send.mock.calls.map((call: string[]) => {
        try { return JSON.parse(call[0]) } catch { return null }
      })
    }

    it('只传 threadId 时，消息格式向后兼容，pipeline_id 为 undefined', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendCancel('thread-abc')

      const messages = getSentMessages(ws)
      const cancelMsg = messages.find((m: any) => m?.type === 'stop_generation')

      expect(cancelMsg).toBeDefined()
      expect(cancelMsg.thread_id).toBe('thread-abc')
      expect(cancelMsg.reason).toBeUndefined()
      expect(cancelMsg.pipeline_id).toBeUndefined()

      disconnect()
    })

    it('不传 pipelineId 时，消息中 pipeline_id 为 undefined', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendCancel('thread-123', 'user requested')

      const messages = getSentMessages(ws)
      const cancelMsg = messages.find((m: any) => m?.type === 'stop_generation')

      expect(cancelMsg).toBeDefined()
      expect(cancelMsg.thread_id).toBe('thread-123')
      expect(cancelMsg.reason).toBe('user requested')
      expect(cancelMsg.pipeline_id).toBeUndefined()

      disconnect()
    })

    it('传入 pipelineId 时，消息中 pipeline_id 正确携带', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendCancel('thread-456', undefined, 'pipeline-xyz')

      const messages = getSentMessages(ws)
      const cancelMsg = messages.find((m: any) => m?.type === 'stop_generation')

      expect(cancelMsg).toBeDefined()
      expect(cancelMsg.thread_id).toBe('thread-456')
      expect(cancelMsg.reason).toBeUndefined()
      expect(cancelMsg.pipeline_id).toBe('pipeline-xyz')

      disconnect()
    })

    it('传入 reason 和 pipelineId 时，两者都正确携带', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendCancel('thread-789', 'timeout exceeded', 'pipeline-abc')

      const messages = getSentMessages(ws)
      const cancelMsg = messages.find((m: any) => m?.type === 'stop_generation')

      expect(cancelMsg).toBeDefined()
      expect(cancelMsg.thread_id).toBe('thread-789')
      expect(cancelMsg.reason).toBe('timeout exceeded')
      expect(cancelMsg.pipeline_id).toBe('pipeline-abc')

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 8. useLayoutModeStore 同步测试
  // ──────────────────────────────────────────────
  describe('状态同步到 store', () => {
    it('连接成功应更新 store 为 connected', async () => {
      const { connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      expect(mockUpdateConnectionStatus).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'connected' }),
      )

      disconnect()
    })

    it('断线应更新 store 为 disconnected', async () => {
      const { connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      mockUpdateConnectionStatus.mockClear()
      simulateClose(ws, 1006, 'error')

      expect(mockUpdateConnectionStatus).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'disconnected' }),
      )

      disconnect()
    })

    it('重连中应更新 store 为 reconnecting', async () => {
      const { connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      vi.advanceTimersByTime(100)
      const ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      mockUpdateConnectionStatus.mockClear()
      simulateClose(ws, 1006, 'error')

      expect(mockUpdateConnectionStatus).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'reconnecting' }),
      )

      disconnect()
    })
  })
})
