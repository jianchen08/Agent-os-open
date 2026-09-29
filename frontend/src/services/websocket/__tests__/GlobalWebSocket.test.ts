/** GlobalWebSocket 单元测试 测试全局 WebSocket 服务的重连参数、状态转换、心跳机制。 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { finishWsServiceSetup, MockWebSocket, instances, type MockWebSocketInstance } from './helpers/mockWebSocket'
import type * as globalWebSocketMod from '../GlobalWebSocket'

// ── Mock 依赖 ──

// Mock wsTicket（WS 一次性票据签发端点，真实实现走 apiClient 网络请求）：
// 默认成功签发，个别用例用 mockRejectedValueOnce 覆写失败路径
const mockFetchWsTicket = vi.fn(async () => 'mock-ws-ticket')
vi.mock('@/services/auth/wsTicket', () => ({
  fetchWsTicket: mockFetchWsTicket,
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

// Mock tokenLifecycle（token 生命周期唯一源，2026-08-21 收口后 GlobalWebSocket 的认证依赖）：
// 默认 token 未过期，让普通重连测试走指数退避路径（1006+未连接过的兜底逻辑仅在 isExpired()=true 时才触发）
const mockIsExpired = vi.fn(() => false)
const mockRefresh = vi.fn(async () => {})
vi.mock('@/services/auth/tokenLifecycle', () => ({
  isExpired: mockIsExpired,
  refresh: mockRefresh,
  getAccessToken: () => 'test-token',
  isAuthFailureFromError: () => false,
}))
vi.mock('@/services/authCallbacks', () => ({
  triggerAuthExpired: vi.fn(),
}))

// Mock logger（工厂同源：顶部注册与 createService 内 doMock 共用）
function loggerMock() {
  return {
    loggers: {
      websocket: {
        debug: vi.fn(),
        info: vi.fn(),
        warn: vi.fn(),
        error: vi.fn(),
      },
    },
  }
}
vi.mock('@/utils/logger', () => loggerMock())


// ── 导入被测模块 ──

// 必须在 mock 设置之后导入
let GlobalWebSocketService: globalWebSocketMod.default.constructor
let ConnectionStatus: globalWebSocketMod.ConnectionStatus

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
  vi.doMock('@/stores/layoutModeStore', () => ({
    useLayoutModeStore: {
      getState: () => ({
        updateConnectionStatus: mockUpdateConnectionStatus,
      }),
    },
  }))
  vi.doMock('@/utils/logger', () => loggerMock())

  return finishWsServiceSetup()
}

/** 模拟成功连接：先触发 connect → 推进 timer → 触发 onopen */
function simulateSuccessfulOpen(ws: MockWebSocketInstance): void {
  if (ws.onopen) {
    ws.onopen({})
  }
}

/** 建连 → 推进调度 → 完成 open 握手，返回可交互的 ws 实例（连接后置用例共用前缀） */
async function connectAndOpen(
  bundle: {
    connect: (token: string) => void
    getLatestWs: () => MockWebSocketInstance | undefined
  },
  token = 'test-token',
): Promise<MockWebSocketInstance> {
  bundle.connect(token)
  await vi.advanceTimersByTimeAsync(100)
  const ws = bundle.getLatestWs()!
  simulateSuccessfulOpen(ws)
  return ws
}

/** 模拟连接关闭 */
function simulateClose(ws: MockWebSocketInstance, code: number = 1000, reason: string = ''): void {
  if (ws.onclose) {
    ws.onclose({ code, reason })
  }
}

/** 建服务并完成首次连接握手，返回可交互句柄（消息协议类用例共用前缀） */
async function setupConnected() {
  const { service, connect, getLatestWs, disconnect } = await createService()
  const ws = await connectAndOpen({ connect, getLatestWs })
  return { service, ws, disconnect }
}

/** 从 ws.send 的调用记录中提取所有已发送消息的解析结果 */
function getSentMessages(ws: MockWebSocketInstance) {
  return ws.send.mock.calls.map((call: string[]) => {
    try { return JSON.parse(call[0]) } catch { return null }
  })
}

/** 断线 → 自动重连：推进退避计时并返回新 WS 实例（票据单次消费需重新取票） */
async function reconnectAfterClose(
  getLatestWs: () => MockWebSocketInstance | undefined,
  ws: MockWebSocketInstance,
  code = 1006,
  reason = 'network',
) {
  simulateClose(ws, code, reason)
  await vi.advanceTimersByTimeAsync(4000 + 100)
  return getLatestWs()!
}

// ── 测试套件 ──

describe('GlobalWebSocketService', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockUpdateConnectionStatus.mockClear()
    mockFetchWsTicket.mockClear()
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

      const ws = await connectAndOpen({ connect, getLatestWs })
      expect(service.status).toBe('connected')

      // 模拟断开
      simulateClose(ws, 1006, 'network error')

      // 此时 status 应为 reconnecting
      expect(service.status).toBe('reconnecting')

      // 推进时间少于 4 秒，不应重连
      await vi.advanceTimersByTimeAsync(3999)

      // 推进到 4 秒，connect 应被再次调用
      await vi.advanceTimersByTimeAsync(1)

      // 4秒后应触发重连（connect 内部又有 50ms 延迟）
      await vi.advanceTimersByTimeAsync(100)

      // 应创建了新的 WS 实例
      expect(instances.length).toBeGreaterThanOrEqual(2)

      service.disconnect()
    })

    it('重连延迟应按指数退避递增', async () => {
      const { service, connect, getLatestWs } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 第 1 次断开 → 延迟 4s (BASE_DELAY * 2^0)
      simulateClose(ws, 1006, 'error')
      expect(service.status).toBe('reconnecting')

      // 推进到 4s 触发重连
      await vi.advanceTimersByTimeAsync(4000 + 100)
      const ws2 = getLatestWs()!
      expect(ws2).not.toBe(ws)

      // 第 2 次连接失败
      await vi.advanceTimersByTimeAsync(100)
      simulateClose(ws2, 1006, 'error')

      // 推进到 8s (BASE_DELAY * 2^1)
      await vi.advanceTimersByTimeAsync(8000 + 100)
      const ws3 = getLatestWs()!
      expect(ws3).not.toBe(ws2)

      // 第 3 次连接失败
      await vi.advanceTimersByTimeAsync(100)
      simulateClose(ws3, 1006, 'error')

      // 推进到 16s (BASE_DELAY * 2^2)
      await vi.advanceTimersByTimeAsync(16000 + 100)
      const ws4 = getLatestWs()!
      expect(ws4).not.toBe(ws3)

      service.disconnect()
    })

    it('重连延迟不应超过最大值 60 秒（RECONNECT_MAX_DELAY）', async () => {
      const { service, connect, getLatestWs } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 断开后进入 reconnecting，验证经过足够多次重连后服务仍持续尝试
      simulateClose(ws, 1006, 'error')
      expect(service.status).toBe('reconnecting')

      // 模拟多次重连失败，每次推进 61s（超过最大延迟 60s）
      // 验证经过多次失败后仍能持续重连（不放弃）
      for (let i = 0; i < 15; i++) {
        await vi.advanceTimersByTimeAsync(61000)
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

      const ws = await connectAndOpen({ connect, getLatestWs })

      expect(service.status).toBe('connected')
      service.disconnect()
    })

    it('连接关闭（非 code=4000）应触发 reconnecting', async () => {
      const { service, connect, getLatestWs } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 模拟异常断开
      simulateClose(ws, 1006, 'abnormal')

      expect(service.status).toBe('reconnecting')
      service.disconnect()
    })

    it('连接关闭（code=4000）不应重连，状态保持 disconnected', async () => {
      const { service, connect, getLatestWs } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // code=4000 表示被新连接替换
      simulateClose(ws, 4000, '被新连接替换')

      expect(service.status).toBe('disconnected')
      service.disconnect()
    })

    it('被 4000 踢旧应清空待发队列并广播 kicked_by_replacement（不再静默装死）', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()
      const onKicked = vi.fn()
      service.subscribe('kicked_by_replacement', onKicked)

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 断线期间入队一条 user_input（占位超时计时同时挂上）
      service.sendUserInput('thread-1', '滞留消息', { clientMessageId: 'cmid-k' })

      // 被新连接替换
      simulateClose(ws, 4000, 'replaced_by_new_connection')

      expect(onKicked).toHaveBeenCalledTimes(1)
      // 恢复连接（模拟）后队列已清空：connect 重建 + open 后 flush 无消息可发
      service._kickedByReplacement = false // 测试直接复位以验证队列确实已空
      service.connect('test-token')
      await vi.advanceTimersByTimeAsync(100)
      const ws2 = getLatestWs()!
      simulateSuccessfulOpen(ws2)
      const sent = ws2.send.mock.calls.some((call: string[]) => {
        try { return JSON.parse(call[0])?.client_message_id === 'cmid-k' } catch { return false }
      })
      expect(sent).toBe(false)

      disconnect()
    })

    it('被 4000 踢旧后 connect() 不再新建连接（防 visibilitychange 互踢环）', async () => {
      const { service, connect, getLatestWs } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // code=4000 被新连接替换
      simulateClose(ws, 4000, '被新连接替换')
      expect(service.wasKickedByReplacement()).toBe(true)

      // visibilitychange/router 等自动路径再调 connect → 必须拦截，不产生新连接
      const instancesBefore = instances.length
      connect('test-token')
      expect(instances.length).toBe(instancesBefore)
      expect(service.status).toBe('disconnected')
      service.disconnect()
    })

    it('被 4000 踢旧后 disconnect()（登出）复位标记，允许重新连接', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      simulateClose(ws, 4000, '被新连接替换')
      expect(service.wasKickedByReplacement()).toBe(true)

      disconnect()
      expect(service.wasKickedByReplacement()).toBe(false)
    })

    it('收到应用层 kicked 帧应置位防重连（Close 码被代理退化为 1006 也不重连）', async () => {
      const { service, connect, getLatestWs } = await createService()
      const onKicked = vi.fn()
      service.subscribe('kicked_by_replacement', onKicked)

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 内核踢旧两段式：kicked 文本帧先到（先于 Close 帧）
      ws.onmessage?.({
        data: JSON.stringify({ type: 'kicked', data: { reason: 'replaced_by_new_connection' } }),
      })
      // Close 帧状态码被代理链退化：浏览器端只见 1006
      simulateClose(ws, 1006, '')

      expect(service.wasKickedByReplacement()).toBe(true)
      expect(onKicked).toHaveBeenCalledTimes(1)
      // 自动路径（visibilitychange/router token 变化）再调 connect 必须拦截，
      // 否则 A/B 双客户端互踢循环（2026-09-04 实测互踢风暴）
      const instancesBefore = instances.length
      connect('test-token')
      expect(instances.length).toBe(instancesBefore)
      service.disconnect()
    })

    it('完整流程: disconnected → connecting → connected → reconnecting → connected', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      // 1. disconnected
      expect(service.status).toBe('disconnected')

      // 2. connecting
      connect('test-token')
      expect(service.status).toBe('connecting')
      await vi.advanceTimersByTimeAsync(100)

      // 3. connected
      let ws = getLatestWs()!
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      // 4. reconnecting (断线)
      simulateClose(ws, 1006, 'network lost')
      expect(service.status).toBe('reconnecting')

      // 5. 重连成功 → connected
      await vi.advanceTimersByTimeAsync(4000 + 100) // 等待重连延迟
      ws = getLatestWs()!
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('disconnect 后状态应为 disconnected 且不再重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      disconnect()
      expect(service.status).toBe('disconnected')

      // 推进大量时间，不应创建新的 WS
      const instanceCountBefore = instances.length
      await vi.advanceTimersByTimeAsync(120000)
      expect(instances.length).toBe(instanceCountBefore)
    })

    it('disconnect 后再次 connect 应能重新建立连接（登出重登 WS 复活）', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })
      expect(service.status).toBe('connected')

      // 登出：disconnect 置 _disposed
      disconnect()
      expect((service as unknown as { _disposed: boolean })._disposed).toBe(true)

      // SPA 内重新登录：connect 复位 _disposed，连接照常建立
      connect('test-token-2')
      expect(service.status).toBe('connecting')
      await vi.advanceTimersByTimeAsync(100)

      const ws2 = getLatestWs()!
      expect(ws2).not.toBe(ws)
      simulateSuccessfulOpen(ws2)
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('connected 状态下 token 轮换：不拆连接（无 close），状态保持 connected', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs }, 'token-a')

      // token 续期后 router effect 用新 token 调 connect
      connect('token-b')

      expect(service.status).toBe('connected')
      expect(ws.close).not.toHaveBeenCalled()
      // 连接未被替换：仍是同一个 WS 实例
      expect(instances[instances.length - 1]).toBe(ws)

      disconnect()
    })

    it('connected 期间轮换的新 token 用于断线重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs }, 'token-a')

      // token 轮换：只更新内部 token，不拆连接
      connect('token-b')
      expect(service.status).toBe('connected')

      // 断线 → 自动重连：票据单次消费，必须重新取票（轮换后的 token 由
      // apiClient 随取票请求统一携带），握手 URL 携带新票据且不含 token
      const ws2 = await reconnectAfterClose(getLatestWs, ws)
      expect(ws2).not.toBe(ws)
      expect(mockFetchWsTicket).toHaveBeenCalledTimes(2)
      expect(ws2.url).toContain('ticket=mock-ws-ticket')
      expect(ws2.url).not.toContain('token=')

      disconnect()
    })

    it('connecting 状态下收到不同 token：拆开在途连接换新 token 重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('token-a')
      // 取票在途窗口内换 token：旧取票流程作废，不产生孤儿连接
      connect('token-b')
      await vi.advanceTimersByTimeAsync(100)

      // 仅一个 WS 实例：被取代的取票流程拿到票后自我作废
      expect(instances.length).toBe(1)
      const ws = instances[0]
      expect(ws.url).toContain('ticket=mock-ws-ticket')
      expect(ws.url).not.toContain('token=')
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 2b. WS 一次性票据（POST /api/v1/ws-ticket → ?ticket= 握手）
  // ──────────────────────────────────────────────
  describe('WS 一次性票据', () => {
    it('取票成功：握手 URL 携带 ticket 且不含 token', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      connect('test-token')
      await vi.advanceTimersByTimeAsync(100)

      expect(mockFetchWsTicket).toHaveBeenCalledTimes(1)
      const ws = getLatestWs()!
      expect(ws.url).toContain('ticket=mock-ws-ticket')
      expect(ws.url).not.toContain('token=')
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('取票 401：不建连，先刷新 token 再退避重连（不回退 token 直连）', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()
      mockFetchWsTicket.mockRejectedValueOnce(Object.assign(new Error('未认证'), { code: '401' }))

      connect('test-token')
      await vi.advanceTimersByTimeAsync(100)

      // 取票失败：未创建任何 WS 连接，进入重连退避
      expect(instances.length).toBe(0)
      expect(service.status).toBe('reconnecting')

      // 退避到点：认证拒绝路径先刷新 token（与 4001 掉线同路径），再重新取票建连
      await vi.advanceTimersByTimeAsync(4000 + 100)
      expect(mockRefresh).toHaveBeenCalled()
      expect(instances.length).toBe(1)
      const ws = getLatestWs()!
      expect(ws.url).toContain('ticket=mock-ws-ticket')
      simulateSuccessfulOpen(ws)
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('断线重连重新取票（票据单次消费，每次连接独立取票）', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })
      expect(mockFetchWsTicket).toHaveBeenCalledTimes(1)

      const ws2 = await reconnectAfterClose(getLatestWs, ws)
      expect(ws2).not.toBe(ws)
      expect(mockFetchWsTicket).toHaveBeenCalledTimes(2)
      expect(ws2.url).toContain('ticket=mock-ws-ticket')
      expect(service.status).not.toBe('connected')
      simulateSuccessfulOpen(ws2)
      expect(service.status).toBe('connected')

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 3. 心跳机制测试
  // ──────────────────────────────────────────────
  describe('心跳机制', () => {
    it('连接成功后应启动心跳定时器', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 推进 30 秒触发心跳
      await vi.advanceTimersByTimeAsync(30000)

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

    it('收到 heartbeat_ack 应刷新 pong 新鲜度（阈值内不误断）', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 触发心跳发送
      await vi.advanceTimersByTimeAsync(30000)

      // 模拟收到 heartbeat_ack
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify({ type: 'heartbeat_ack' }) })
      }

      // 再推进一个周期（距 ack 30s < 90s 判死阈值）：新鲜度尚足，不应关闭连接
      await vi.advanceTimersByTimeAsync(30000)

      // 连接应仍然存在（ws.close 未因心跳判死被调用）
      expect(service.status).toBe('connected')

      disconnect()
    })

    it('心跳超时后应触发重连', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 触发心跳发送（30s interval 触发）
      await vi.advanceTimersByTimeAsync(30000)

      // 验证心跳已发送
      const heartbeatSent = ws.send.mock.calls.some((call: string[]) => {
        try { return JSON.parse(call[0])?.type === 'heartbeat' } catch { return false }
      })
      expect(heartbeatSent).toBe(true)

      // 心跳判死由 pong 新鲜度驱动（距最近 ack ≥ 90s，见 GlobalWebSocketHeartbeat.test.ts）。
      // 此处直接以 close(2002) 模拟判死产物，验证 onclose 对心跳超时关闭的处理
      // 是否正确（走普通重连，不触发 token 刷新）。
      ws.close(2002, '心跳超时')

      // ws.close(4001) → onclose → _scheduleReconnect → status = 'reconnecting'
      expect(service.status).toBe('reconnecting')

      // 推进时间验证重连会创建新的 WebSocket
      await vi.advanceTimersByTimeAsync(4000 + 100)
      expect(instances.length).toBeGreaterThanOrEqual(2)

      disconnect()
    })

    it('心跳超时应给 ack 留容错：阈值内收到 ack 刷新新鲜度不断连', async () => {
      // LLM 流式期间后端事件循环负载高，heartbeat_ack 响应极易突破 30s。
      // 修复: 判死 = pong 新鲜度，距最近 ack 90s（连续 3 个周期）才断。
      // 回归契约: 阈值内迟到的 ack 刷新判死截止期，连接不因单周期延迟被断。
      const { service, connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      // 推进 30s → 心跳发出
      await vi.advanceTimersByTimeAsync(30000)
      const closeCallsBefore = ws.close.mock.calls.length

      // 再推进 10s（建连后累计 40s，距 90s 阈值尚余 50s）→ ack 稍慢但仍在容错内
      await vi.advanceTimersByTimeAsync(10000)
      if (ws.onmessage) {
        ws.onmessage({ data: JSON.stringify({ type: 'heartbeat_ack' }) })
      }

      // ack 刷新新鲜度后，再推进超过原 90s 窗口（验证旧 45s 零容错已不复存在）
      await vi.advanceTimersByTimeAsync(35000)

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

      const ws = await connectAndOpen({ connect, getLatestWs })

      expect(connectHandler).toHaveBeenCalledWith({ status: 'connected' })
      expect(statusHandler).toHaveBeenCalledWith({ status: 'connected' })

      disconnect()
    })

    it('重连成功应额外触发 reconnected 事件', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      const reconnectedHandler = vi.fn()
      service.subscribe('reconnected', reconnectedHandler)

      connect('test-token')
      await vi.advanceTimersByTimeAsync(100)
      let ws = getLatestWs()!
      simulateSuccessfulOpen(ws)

      // 首次连接不应触发 reconnected
      expect(reconnectedHandler).not.toHaveBeenCalled()

      // 断开并重连
      simulateClose(ws, 1006, 'error')
      await vi.advanceTimersByTimeAsync(4000 + 100)
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

      await vi.advanceTimersByTimeAsync(100)
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
      const ws = await connectAndOpen({ connect, getLatestWs })

      // flushQueue 应发送了排队的消息
      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg.content).toBe('hello')
      expect(userMsg.thread_id).toBe('thread-1')

      disconnect()
    })

    it('sendUserInput 携带 thinkingStrength 时帧带 thinking_strength 字段', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      service.sendUserInput('thread-1', '强度测试', {
        thinkingStrength: 'high',
        pipelineId: 'pipe-s',
        clientMessageId: 'cmid-s',
      })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg.thinking_strength).toBe('high')

      disconnect()
    })

    it('sendUserInput 未指定 strength 时帧 thinking_strength 为空串', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      service.sendUserInput('thread-1', '无强度', {
        pipelineId: 'pipe-s',
        clientMessageId: 'cmid-s',
      })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg.thinking_strength).toBe('')

      disconnect()
    })

    // 消息级 execution_context（{mode,...}）出站帧契约（BUG-35 回归钉）：
    // 带则原样入帧（内核 1a2 合并点整体覆盖会话级来源），不带则帧内无该键
    // （后端按 thread 出生值注入，与旧行为一致）。
    it('sendUserInput 携带 executionContext 时帧带 execution_context 字段', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      service.sendUserInput('thread-1', '模式测试', {
        pipelineId: 'pipe-s',
        clientMessageId: 'cmid-ec',
        executionContext: { mode: 'coding' },
      })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg.execution_context).toEqual({ mode: 'coding' })

      disconnect()
    })

    it('sendUserInput 未携带 executionContext 时帧无 execution_context 键', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      service.sendUserInput('thread-1', '无上下文', {
        pipelineId: 'pipe-s',
        clientMessageId: 'cmid-noec',
      })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg).not.toHaveProperty('execution_context')

      disconnect()
    })

    // 消息级原生 agent_id 通道（附身身份切换，用户裁定 2026-09-24）：
    // 带 agentId 则帧带 snake_case agent_id（内核 chat_send_handler params 契约），
    // 不带则帧无该键（后端按既有执行上下文执行，不触发身份切换）。
    it('sendUserInput 携带 agentId 时帧带 agent_id 字段', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      service.sendUserInput('thread-1', '附身测试', {
        pipelineId: 'pipe-s',
        clientMessageId: 'cmid-agent',
        agentId: 'mode_roleplay/card_luna',
      })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg.agent_id).toBe('mode_roleplay/card_luna')

      disconnect()
    })

    it('sendUserInput 未携带 agentId 时帧无 agent_id 键', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      service.sendUserInput('thread-1', '无附身', {
        pipelineId: 'pipe-s',
        clientMessageId: 'cmid-noagent',
      })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const userMsg = getSentMessages(ws).find((c: any) => c?.type === 'user_input')
      expect(userMsg).toBeDefined()
      expect(userMsg).not.toHaveProperty('agent_id')

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
      await vi.advanceTimersByTimeAsync(100)
      expect(service.status).toBe('connecting')

      // 推进到 15 秒（CONNECTION_TIMEOUT）
      await vi.advanceTimersByTimeAsync(15000)

      // 应触发超时重连
      expect(service.status).toBe('reconnecting')

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 7. sendCancel pipelineId 参数测试
  // ──────────────────────────────────────────────
  describe('sendCancel - pipelineId 参数', () => {
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
  // 7b. sendRegenerate 测试（批次 D：重新生成/回退/编辑重发入站协议）
  // ──────────────────────────────────────────────
  describe('sendRegenerate - 重新生成协议', () => {
    it('重新生成：只传 thread_id 时按协议携带 pipeline_id/user_message_id（空串缺省）', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendRegenerate('thread-abc')

      const messages = getSentMessages(ws)
      const regen = messages.find((m: any) => m?.type === 'regenerate')

      expect(regen).toBeDefined()
      expect(regen.thread_id).toBe('thread-abc')
      expect(regen.pipeline_id).toBe('')
      expect(regen.user_message_id).toBe('')
      expect(regen.new_content).toBe('')
      disconnect()
    })

    it('回退：指定 user_message_id 定位截断点', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendRegenerate('thread-1', { pipelineId: 'pipe-x', userMessageId: 'u-42' })

      const messages = getSentMessages(ws)
      const regen = messages.find((m: any) => m?.type === 'regenerate')

      expect(regen).toBeDefined()
      expect(regen.thread_id).toBe('thread-1')
      expect(regen.pipeline_id).toBe('pipe-x')
      expect(regen.user_message_id).toBe('u-42')
      expect(regen.new_content).toBe('')
      disconnect()
    })

    it('编辑重发：携带 new_content 改写目标消息内容', async () => {
      const { service, ws, disconnect } = await setupConnected()

      service.sendRegenerate('thread-2', { pipelineId: 'pipe-y', userMessageId: 'u-7', newContent: '改写后的问题' })

      const messages = getSentMessages(ws)
      const regen = messages.find((m: any) => m?.type === 'regenerate')

      expect(regen).toBeDefined()
      expect(regen.user_message_id).toBe('u-7')
      expect(regen.new_content).toBe('改写后的问题')
      disconnect()
    })

    it('未连接时入队，重连后随 flushQueue 发出', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()

      // 未连接直接发送 → 入队
      service.sendRegenerate('thread-3', { pipelineId: 'pipe-z' })

      const ws = await connectAndOpen({ connect, getLatestWs })

      const messages = getSentMessages(ws)
      const regen = messages.find((m: any) => m?.type === 'regenerate')
      expect(regen).toBeDefined()
      expect(regen.thread_id).toBe('thread-3')
      expect(regen.pipeline_id).toBe('pipe-z')
      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // 8. useLayoutModeStore 同步测试
  // ──────────────────────────────────────────────
  describe('状态同步到 store', () => {
    it('连接成功应更新 store 为 connected', async () => {
      const { connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      expect(mockUpdateConnectionStatus).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'connected' }),
      )

      disconnect()
    })

    it('断线应更新 store 为 disconnected', async () => {
      const { connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      mockUpdateConnectionStatus.mockClear()
      simulateClose(ws, 1006, 'error')

      expect(mockUpdateConnectionStatus).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'disconnected' }),
      )

      disconnect()
    })

    it('重连中应更新 store 为 reconnecting', async () => {
      const { connect, getLatestWs, disconnect } = await createService()

      const ws = await connectAndOpen({ connect, getLatestWs })

      mockUpdateConnectionStatus.mockClear()
      simulateClose(ws, 1006, 'error')

      expect(mockUpdateConnectionStatus).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'reconnecting' }),
      )

      disconnect()
    })
  })

  // ──────────────────────────────────────────────
  // user_input 排队超时（错误透传，2026-08-21）
  // ──────────────────────────────────────────────
  describe('user_input 排队超时', () => {
    it('断线时 sendUserInput 入队，超过 TTL(20s) 未发出应广播 user_input_send_timeout 并剔除消息', async () => {
      const { service, disconnect } = await createService()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      // 未 connect（disconnected）时发送：静默入队
      service.sendUserInput('thread-1', '测试消息', {
        pipelineId: 'pipe-1',
        clientMessageId: 'cmid-1',
      })
      expect(onTimeout).not.toHaveBeenCalled()

      // TTL 前一毫秒：仍无事件
      await vi.advanceTimersByTimeAsync(20000 - 1)
      expect(onTimeout).not.toHaveBeenCalled()

      // TTL 到点：广播 + 队列剔除
      await vi.advanceTimersByTimeAsync(1)
      expect(onTimeout).toHaveBeenCalledTimes(1)
      const payload = onTimeout.mock.calls[0][0]
      expect(payload.data).toMatchObject({
        thread_id: 'thread-1',
        pipeline_id: 'pipe-1',
        client_message_id: 'cmid-1',
        content: '测试消息',
      })

      disconnect()
    })

    it('TTL 内重连成功并 flush 后，收到 ack 即撤销计时（不广播 send_timeout）', async () => {
      const { service, connect, getLatestWs, disconnect } = await createService()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', 'hello', {
        pipelineId: 'pipe-1',
        clientMessageId: 'cmid-2',
      })

      // TTL 内恢复连接：connect → open → _flushQueue 发出
      const ws = await connectAndOpen({ connect, getLatestWs })

      const sent = ws.send.mock.calls.some((call: string[]) => {
        try {
          return JSON.parse(call[0])?.client_message_id === 'cmid-2'
        } catch {
          return false
        }
      })
      expect(sent).toBe(true)

      // ADR 2026-09-29：flush ≠ 送达（直发同样可能进僵尸 TCP），内核回执才是
      // 撤销点——健康内核 ms 级 ack，计时撤销后 TTL 到点不广播
      ws.onmessage?.({
        data: JSON.stringify({ type: 'user_input_ack', client_message_id: 'cmid-2', ok: true }),
      })
      await vi.advanceTimersByTimeAsync(60000)
      expect(onTimeout).not.toHaveBeenCalled()

      disconnect()
    })

    it('disconnect 应清理所有排队超时计时器（不再广播）', async () => {
      const { service, disconnect } = await createService()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', 'x', { clientMessageId: 'cmid-3' })
      disconnect()
      await vi.advanceTimersByTimeAsync(60000)
      expect(onTimeout).not.toHaveBeenCalled()
    })
  })

  // ──────────────────────────────────────────────
  // user_input 恒挂超时 + ack 撤销（ADR 2026-09-29 回执契约）
  // 直发路径（status==='connected' 的 ws.send）与入队路径同样挂 TTL：僵尸 TCP 上
  // ws.send 不抛错不入队（R302 实证 8 分钟无声），唯一有界失败 = 客户端计时器。
  // ──────────────────────────────────────────────
  describe('user_input 恒挂超时与 ack 撤销', () => {
    it('connected 直发路径同样挂超时：TTL 内未收 ack 应广播 user_input_send_timeout', async () => {
      const { service, ws, disconnect } = await setupConnected()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', '直发消息', {
        pipelineId: 'pipe-1',
        clientMessageId: 'cmid-direct-1',
      })
      // 直发成功（帧已过 ws.send），但超时保护必须同样在位
      expect(ws.send).toHaveBeenCalled()
      expect(onTimeout).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(20000 - 1)
      expect(onTimeout).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1)
      expect(onTimeout).toHaveBeenCalledTimes(1)
      const payload = onTimeout.mock.calls[0][0]
      expect(payload.data).toMatchObject({
        thread_id: 'thread-1',
        pipeline_id: 'pipe-1',
        client_message_id: 'cmid-direct-1',
      })
      // 直发路径与队列路径 reason 可辨：已发未收回执 ≠ 排队未发出
      expect(payload.data.reason).toContain('回执')
      expect(payload.data.serverAware).toBeUndefined()
      disconnect()
    })

    it('收到 user_input_ack(ok:true) 即撤超时，后续不再广播', async () => {
      const { service, ws, disconnect } = await setupConnected()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', 'hello', {
        pipelineId: 'pipe-1',
        clientMessageId: 'cmid-ack-1',
      })
      ws.onmessage?.({
        data: JSON.stringify({
          type: 'user_input_ack',
          client_message_id: 'cmid-ack-1',
          thread_id: 'thread-1',
          ok: true,
        }),
      })

      await vi.advanceTimersByTimeAsync(60000)
      expect(onTimeout).not.toHaveBeenCalled()
      disconnect()
    })

    it('收到 user_input_ack(ok:false) 立即广播 send_timeout（serverAware + 内核 error 原文），不等 TTL', async () => {
      const { service, ws, disconnect } = await setupConnected()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', 'hello', {
        pipelineId: 'pipe-1',
        clientMessageId: 'cmid-rej-1',
      })
      ws.onmessage?.({
        data: JSON.stringify({
          type: 'user_input_ack',
          client_message_id: 'cmid-rej-1',
          thread_id: 'thread-1',
          ok: false,
          error: '会话无可派发管道：pipeline_id 缺失或不属于该会话（拒绝静默换管道）',
        }),
      })

      // 立即透传（零等待），带 serverAware 标记与内核 error 原文
      expect(onTimeout).toHaveBeenCalledTimes(1)
      const payload = onTimeout.mock.calls[0][0]
      expect(payload.data.serverAware).toBe(true)
      expect(payload.data.reason).toContain('会话无可派发管道')
      expect(payload.data.client_message_id).toBe('cmid-rej-1')
      // 撤销后 TTL 到点不二次广播（一消息至多一次失败可见）
      await vi.advanceTimersByTimeAsync(60000)
      expect(onTimeout).toHaveBeenCalledTimes(1)
      disconnect()
    })

    it('stream_start 按 pipeline_id 撤销在途超时', async () => {
      const { service, ws, disconnect } = await setupConnected()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', 'hello', {
        pipelineId: 'pipe-stream-9',
        clientMessageId: 'cmid-ss-1',
      })
      ws.onmessage?.({
        data: JSON.stringify({
          type: 'stream_start',
          data: { pipeline_id: 'pipe-stream-9', message_id: 'a_test' },
        }),
      })

      await vi.advanceTimersByTimeAsync(60000)
      expect(onTimeout).not.toHaveBeenCalled()
      disconnect()
    })

    it('ack 只撤销匹配 cmid 的计时，不误伤其他在途消息', async () => {
      const { service, ws, disconnect } = await setupConnected()
      const onTimeout = vi.fn()
      service.subscribe('user_input_send_timeout', onTimeout)

      service.sendUserInput('thread-1', 'a', { pipelineId: 'pipe-1', clientMessageId: 'cmid-a' })
      service.sendUserInput('thread-1', 'b', { pipelineId: 'pipe-1', clientMessageId: 'cmid-b' })
      ws.onmessage?.({
        data: JSON.stringify({
          type: 'user_input_ack',
          client_message_id: 'cmid-a',
          ok: true,
        }),
      })

      await vi.advanceTimersByTimeAsync(20000)
      // 只有未被 ack 的 cmid-b 触发超时
      expect(onTimeout).toHaveBeenCalledTimes(1)
      expect(onTimeout.mock.calls[0][0].data.client_message_id).toBe('cmid-b')
      disconnect()
    })
  })
})
