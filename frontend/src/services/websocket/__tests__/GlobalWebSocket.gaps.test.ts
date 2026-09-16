// @feature: FP-T12 GlobalWebSocket 缺口补测 | @ci: frontend-test
/**
 * GlobalWebSocketService 缺口补测（与 GlobalWebSocket.test.ts 互补，避免用例重复）：
 * - token 刷新窗口期的 connect 拦截 / 刷新成功换新 token 重连
 * - 断线补漏游标（sequence 追踪 + setLastSequence）
 * - resync_required 转发、非 JSON 帧忽略、handler 异常隔离
 * - 通知/审批/子代理/人类交互四类发送帧
 * - 发送面有界失败：bufferedAmount 背压入队、send 抛错入队、断线去重、flush 失败回队
 * - 重连退避 60s 封顶（超过最大次数后恒定间隔）
 * - 登出后悬挂的陈旧重连计时器不得复活连接
 * - refresh 失败分流：真失效登出停机 / 瞬时故障等下轮
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { finishWsServiceSetup, MockWebSocket, instances, type MockWebSocketInstance } from './helpers/mockWebSocket'

// ── Mock 外部依赖（hoisted 供 vi.mock 工厂与用例共同操控） ──

const { mockFetchWsTicket, mockRefresh, mockGetAccessToken, mockIsExpired, mockIsAuthFailureFromError, mockTriggerAuthExpired, mockUpdateConnectionStatus } = vi.hoisted(() => ({
  mockFetchWsTicket: vi.fn(),
  mockRefresh: vi.fn(),
  mockGetAccessToken: vi.fn(),
  mockIsExpired: vi.fn(),
  mockIsAuthFailureFromError: vi.fn(),
  mockTriggerAuthExpired: vi.fn(),
  mockUpdateConnectionStatus: vi.fn(),
}))

vi.mock('@/services/auth/wsTicket', () => ({
  fetchWsTicket: mockFetchWsTicket,
}))

vi.mock('@/services/auth/tokenLifecycle', () => ({
  isExpired: mockIsExpired,
  refresh: mockRefresh,
  getAccessToken: mockGetAccessToken,
  isAuthFailureFromError: mockIsAuthFailureFromError,
}))

vi.mock('@/services/authCallbacks', () => ({
  triggerAuthExpired: mockTriggerAuthExpired,
}))

vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: {
    getState: () => ({ updateConnectionStatus: mockUpdateConnectionStatus }),
  },
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))


// ── 辅助 ──

async function createService() {
  vi.resetModules()
  return finishWsServiceSetup()
}

function simulateOpen(ws: MockWebSocketInstance): void {
  ws.onopen?.({})
}

function simulateClose(ws: MockWebSocketInstance, code = 1000, reason = ''): void {
  ws.onclose?.({ code, reason })
}

function sentPayloads(ws: MockWebSocketInstance): Array<Record<string, any>> {
  return ws.send.mock.calls.map((call: string[]) => {
    try {
      return JSON.parse(call[0])
    } catch {
      return null
    }
  })
}

function authRejectedTicketError(): Error {
  return Object.assign(new Error('未认证'), { code: '401' })
}

describe('GlobalWebSocketService 缺口补测', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    instances.length = 0
    mockFetchWsTicket.mockReset()
    mockFetchWsTicket.mockResolvedValue('ticket-1')
    mockRefresh.mockReset()
    mockRefresh.mockResolvedValue(undefined)
    mockGetAccessToken.mockReset()
    mockGetAccessToken.mockReturnValue('token-a')
    mockIsExpired.mockReset()
    mockIsExpired.mockReturnValue(false)
    mockIsAuthFailureFromError.mockReset()
    mockIsAuthFailureFromError.mockReturnValue(false)
    mockTriggerAuthExpired.mockClear()
    mockUpdateConnectionStatus.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('token 刷新窗口内的外部 connect 被拒；refresh 成功后换新 token 重连', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    mockFetchWsTicket.mockRejectedValueOnce(authRejectedTicketError())

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    expect(instances.length).toBe(0)
    expect(service.status).toBe('reconnecting')

    // refresh 退避窗口内，外部（router/visibilitychange）用过期 token 抢占 connect：必须拦截
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    expect(instances.length).toBe(0)
    expect(service.status).toBe('reconnecting')

    // 退避到点：先 refresh，取到的新 token 与旧 token 不同 → 以新 token 重连取票
    mockGetAccessToken.mockReturnValue('token-b')
    await vi.advanceTimersByTimeAsync(4100)
    expect(mockRefresh).toHaveBeenCalledTimes(1)
    expect(instances.length).toBe(1)
    const ws = getLatestWs()!
    expect(ws.url).toContain('ticket=ticket-1')
    simulateOpen(ws)
    expect(service.status).toBe('connected')

    disconnect()
  })

  it('收到 resync_required 事件：显式 + 按 type 各派发一次（下游 resync.ts 防抖合并此双发）', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    const onResync = vi.fn()
    service.subscribe('resync_required', onResync)
    const payload = { type: 'resync_required', data: { last_sequence: 7 } }
    ws.onmessage?.({ data: JSON.stringify(payload) })

    // 双发是已文档化行为（resync.ts 设计要点：「显式+按 type 各 emit 一次，单条消息即两次」，
    // 消费端以防抖窗口合并），此处钉住该契约防止无意识变更
    expect(onResync).toHaveBeenCalledTimes(2)
    expect(onResync.mock.calls[0][0]).toEqual(payload)
    expect(onResync.mock.calls[1][0]).toEqual(payload)

    disconnect()
  })

  it.each([
    ['截断 JSON', '{"type":"chunk",'],
    ['二进制噪声', '\u0000\u0001ping'],
  ])('非 JSON 帧（%s）被忽略：不派发任何事件、连接不受影响', async (_label, raw) => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    const onAny = vi.fn()
    service.subscribe('*', onAny)

    expect(() => ws.onmessage?.({ data: raw })).not.toThrow()
    expect(onAny).not.toHaveBeenCalled()
    expect(service.status).toBe('connected')

    disconnect()
  })

  it('sequence 游标：嵌套与顶层位置都追踪、只增不减；setLastSequence 不接受回退', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    ws.onmessage?.({ data: JSON.stringify({ type: 'pipeline_delta', data: { sequence: 5 } }) })
    expect(service.lastSequence).toBe(5)

    ws.onmessage?.({ data: JSON.stringify({ type: 'widget_event', sequence: 9 }) })
    expect(service.lastSequence).toBe(9)

    ws.onmessage?.({ data: JSON.stringify({ type: 'pipeline_delta', data: { sequence: 2 } }) })
    expect(service.lastSequence).toBe(9)

    ws.onmessage?.({
      data: JSON.stringify({ type: 'x', sequence: 4, data: { sequence: 11 } }),
    })
    expect(service.lastSequence).toBe(11)

    service.setLastSequence(20)
    expect(service.lastSequence).toBe(20)
    service.setLastSequence(3)
    expect(service.lastSequence).toBe(20)

    disconnect()
  })

  it('sendActiveThread：connected 时发送，未连接时直接丢弃（通知性消息不入队）', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()

    // 未连接：通知直接丢弃
    service.sendActiveThread('t0', 'p0')

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    service.sendActiveThread('t1', 'p1')
    const changed = sentPayloads(ws).filter((m) => m?.type === 'active_thread_changed')
    expect(changed).toEqual([{ type: 'active_thread_changed', thread_id: 't1', pipeline_id: 'p1' }])

    // 断线期间的通知同样丢弃：重连后 flush 不应出现
    simulateClose(ws, 1006, 'net')
    service.sendActiveThread('t2', 'p2')
    await vi.advanceTimersByTimeAsync(4100)
    const ws2 = getLatestWs()!
    simulateOpen(ws2)
    expect(sentPayloads(ws2).some((m) => m?.type === 'active_thread_changed')).toBe(false)

    disconnect()
  })

  it('sendApproval / sendUserInputResponse / sendInteractionResponse 按协议发帧', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    service.sendApproval('t1', 'approve', 'looks good')
    service.sendUserInputResponse('t1', 'exec-1', 'yes')
    service.sendInteractionResponse('t1', 'req-1', { value: 42 })

    const payloads = sentPayloads(ws)
    expect(payloads).toContainEqual({
      type: 'approval',
      thread_id: 't1',
      decision: 'approve',
      reason: 'looks good',
    })
    expect(payloads).toContainEqual({
      type: 'user_input_response',
      thread_id: 't1',
      execution_id: 'exec-1',
      response: 'yes',
    })
    expect(payloads).toContainEqual({
      type: 'interaction_response',
      thread_id: 't1',
      data: { request_id: 'req-1', response: { value: 42 } },
    })

    disconnect()
  })

  it('bufferedAmount 超阈值：消息延迟入队不直发，恢复后随重连补送', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    ws.bufferedAmount = 1_000_001
    service.sendRegenerate('t1', { pipelineId: 'p1' })
    expect(sentPayloads(ws).some((m) => m?.type === 'regenerate')).toBe(false)

    // 背压缓解后断线重连：滞留消息补送且只送一次
    ws.bufferedAmount = 0
    simulateClose(ws, 1006, 'net')
    await vi.advanceTimersByTimeAsync(4100)
    const ws2 = getLatestWs()!
    simulateOpen(ws2)
    const regen = sentPayloads(ws2).filter((m) => m?.type === 'regenerate')
    expect(regen).toHaveLength(1)
    expect(regen[0]).toMatchObject({ thread_id: 't1', pipeline_id: 'p1' })

    disconnect()
  })

  it('ws.send 抛错：消息转入队列不丢失，恢复连接后补送', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    ws.send = vi.fn(() => {
      throw new Error('socket gone')
    })
    expect(() => service.sendApproval('t1', 'approve', 'r')).not.toThrow()

    simulateClose(ws, 1006, 'net')
    await vi.advanceTimersByTimeAsync(4100)
    const ws2 = getLatestWs()!
    simulateOpen(ws2)
    expect(sentPayloads(ws2).filter((m) => m?.type === 'approval')).toHaveLength(1)

    disconnect()
  })

  it('断线入队去重：同 cmid 的 user_input、同 thread 的 approval 各只入队一次，不同消息不受影响', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()

    service.sendUserInput('t1', 'hello', { clientMessageId: 'cmid-dup' })
    service.sendUserInput('t1', 'hello', { clientMessageId: 'cmid-dup' })
    service.sendUserInput('t1', 'hello2', { clientMessageId: 'cmid-2' })
    service.sendApproval('t1', 'approve')
    service.sendApproval('t1', 'approve')
    service.sendApproval('t2', 'approve')

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    const payloads = sentPayloads(ws)
    expect(payloads.filter((m) => m?.client_message_id === 'cmid-dup')).toHaveLength(1)
    expect(payloads.filter((m) => m?.client_message_id === 'cmid-2')).toHaveLength(1)
    expect(
      payloads.filter((m) => m?.type === 'approval' && m.thread_id === 't1'),
    ).toHaveLength(1)
    expect(
      payloads.filter((m) => m?.type === 'approval' && m.thread_id === 't2'),
    ).toHaveLength(1)

    disconnect()
  })

  it('flush 时 send 失败：消息回队不丢，恢复后补送一次', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    service.sendUserInput('t1', 'flush-fail', { clientMessageId: 'cmid-f' })

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws1 = getLatestWs()!
    ws1.send = vi.fn(() => {
      throw new Error('not ready')
    })
    // open 触发 flush：send 失败被吞掉（回队 + 停止本轮 flush），连接照常建立
    expect(() => simulateOpen(ws1)).not.toThrow()
    expect(service.status).toBe('connected')

    // 恢复连接后消息补送且只送一次（若未回队则永远丢失，若重复入队则会多发）
    simulateClose(ws1, 1006, 'net')
    await vi.advanceTimersByTimeAsync(4100)
    const ws2 = getLatestWs()!
    simulateOpen(ws2)
    const delivered = sentPayloads(ws2).filter((m) => m?.client_message_id === 'cmid-f')
    expect(delivered).toHaveLength(1)
    expect(delivered[0]).toMatchObject({ type: 'user_input', content: 'flush-fail' })

    disconnect()
  })

  it('handler 抛异常不影响同事件的其他 handler，也不影响连接', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    const bad = vi.fn(() => {
      throw new Error('handler boom')
    })
    const good = vi.fn()
    service.subscribe('connect', bad)
    service.subscribe('connect', good)

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    simulateOpen(getLatestWs()!)

    expect(good).toHaveBeenCalledTimes(1)
    expect(good).toHaveBeenCalledWith({ status: 'connected' })
    expect(service.status).toBe('connected')

    disconnect()
  })

  it('重连次数超过上限后退避封顶 60s：第 31 次重连恰在第 30 次失败 60s 后', async () => {
    const { service, connect, disconnect } = await createService()
    const attemptTimes: number[] = []
    mockFetchWsTicket.mockImplementation(async () => {
      attemptTimes.push(Date.now())
      throw new Error('net down')
    })

    connect('token-a')
    let guard = 0
    while (attemptTimes.length < 30 && guard++ < 100) {
      await vi.advanceTimersByTimeAsync(61000)
    }
    expect(attemptTimes.length).toBe(30)
    // 逐次退避 4s/8s/16s/32s 后，从第 5 次起全部触及 60s 上限
    const gaps = attemptTimes.slice(1).map((t, i) => t - attemptTimes[i])
    expect(gaps.slice(0, 4)).toEqual([4_000, 8_000, 16_000, 32_000])
    expect(Math.min(...gaps.slice(4))).toBe(60_000)

    // 第 31 次重连恰在第 30 次失败 60s 后：差 1ms 不触发、到点必触发
    const remaining = 60_000 - (Date.now() - attemptTimes[29])
    await vi.advanceTimersByTimeAsync(remaining - 1)
    expect(attemptTimes.length).toBe(30)
    await vi.advanceTimersByTimeAsync(1)
    expect(attemptTimes.length).toBe(31)
    expect(attemptTimes[30] - attemptTimes[29]).toBe(60_000)

    disconnect()
  })

  it('disconnect 后悬挂的陈旧重连计时器不复活连接（重复 close 事件场景）', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)
    const ws = getLatestWs()!
    simulateOpen(ws)

    // 代理层重复投递 close：两次掉线各排一个重连计时器，仅最新引用可被 disconnect 清除
    simulateClose(ws, 1006, 'net')
    simulateClose(ws, 1006, 'net')
    disconnect()

    const before = instances.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(instances.length).toBe(before)
    expect(service.status).toBe('disconnected')
    expect(mockFetchWsTicket).toHaveBeenCalledTimes(1)
  })

  it('refresh 真失效（refresh_token 无效）：触发登出并停止重连', async () => {
    const { service, connect, disconnect } = await createService()
    mockFetchWsTicket.mockRejectedValueOnce(authRejectedTicketError())
    mockRefresh.mockRejectedValueOnce(new Error('invalid_grant'))
    mockIsAuthFailureFromError.mockReturnValueOnce(true)

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)

    // 退避到点：refresh 失败且判定为真失效 → 登出，不再有任何重连取票
    await vi.advanceTimersByTimeAsync(4100)
    expect(mockRefresh).toHaveBeenCalledTimes(1)
    expect(mockTriggerAuthExpired).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(120000)
    expect(mockFetchWsTicket).toHaveBeenCalledTimes(1)
    expect(service.status).not.toBe('connected')

    disconnect()
  })

  it('refresh 瞬时失败（网络/超时）：不登出，按退避等下一轮刷新成功后重连', async () => {
    const { service, connect, getLatestWs, disconnect } = await createService()
    mockFetchWsTicket.mockRejectedValueOnce(authRejectedTicketError())
    mockRefresh.mockRejectedValueOnce(new Error('refresh timeout'))

    connect('token-a')
    await vi.advanceTimersByTimeAsync(100)

    // 第一轮 refresh 失败（瞬时故障）→ 不登出，重新排程
    await vi.advanceTimersByTimeAsync(4100)
    expect(mockRefresh).toHaveBeenCalledTimes(1)
    expect(mockTriggerAuthExpired).not.toHaveBeenCalled()

    // 第二轮 refresh 成功（token 未变）→ 直接重连取票
    await vi.advanceTimersByTimeAsync(9000)
    expect(mockRefresh).toHaveBeenCalledTimes(2)
    expect(mockFetchWsTicket).toHaveBeenCalledTimes(2)
    const ws = getLatestWs()!
    simulateOpen(ws)
    expect(service.status).toBe('connected')

    disconnect()
  })
})
