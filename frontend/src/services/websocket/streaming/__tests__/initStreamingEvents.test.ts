/**
 * initStreamingEvents / destroyStreamingEvents / reinitStreamingEvents 测试
 *
 * 验证：
 * 1. init 幂等（重复调用只订阅一次）
 * 2. 订阅了全部 WS_SERVER_EVENTS + reconnected
 * 3. 高频率增量事件（text_delta/reasoning_delta/tool_call_delta/keepalive）不记日志
 * 4. 中央门控：非关注 pipeline 的事件被丢弃（不触发 handler）
 * 5. 无 pipelineId 的会话级/全局事件照常放行
 * 6. destroy 反注册全部并清空；reinit = destroy + init
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

const { mockSubscribe, mockUnsubscribe, mockHandlers } = vi.hoisted(() => ({
  mockSubscribe: vi.fn(),
  mockUnsubscribe: vi.fn(),
  mockHandlers: new Map<string, (data: any) => void>(),
}))

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, handler: (data: any) => void) => {
      mockSubscribe(event, handler)
      mockHandlers.set(event, handler)
    },
    unsubscribe: (event: string, handler: (data: any) => void) => {
      mockUnsubscribe(event, handler)
      if (mockHandlers.get(event) === handler) mockHandlers.delete(event)
    },
  },
}))

const mockLogger = vi.hoisted(() => ({
  debug: vi.fn(),
  info: vi.fn(),
  warn: vi.fn(),
  error: vi.fn(),
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    websocket: mockLogger,
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

// 各 handler 的观测替身：把"事件是否透传到业务 handler"记录到数组
const invoked = vi.hoisted(() => ({ calls: [] as string[] }))

vi.mock('../handlers', () => ({
  handleBlockEnd: () => { invoked.calls.push('block_end') },
  handleBlockStart: () => { invoked.calls.push('block_start') },
  handleFinish: () => { invoked.calls.push('finish') },
  handleNewMessage: () => { invoked.calls.push('new_message') },
  handleReasoningDelta: () => { invoked.calls.push('reasoning_delta') },
  handleStreamEnd: () => { invoked.calls.push('stream_end') },
  handleStreamError: () => { invoked.calls.push('stream_error') },
  handleStreamStart: () => { invoked.calls.push('stream_start') },
  handleTextDelta: () => { invoked.calls.push('text_delta') },
  handleToolCallDelta: () => { invoked.calls.push('tool_call_delta') },
  handleToolProgress: () => { invoked.calls.push('tool_progress') },
  handleToolResult: () => { invoked.calls.push('tool_result') },
  handleToolStart: () => { invoked.calls.push('tool_start') },
  handleUsage: () => { invoked.calls.push('usage') },
  handleIteration: () => { invoked.calls.push('iteration') },
}))

vi.mock('../lifecycleHandlers', () => ({
  handleCostUpdate: () => { invoked.calls.push('cost_update') },
  handleReconnected: () => { invoked.calls.push('reconnected') },
  handleSystemNotification: () => { invoked.calls.push('system_notification') },
  handleTerminationStatus: () => { invoked.calls.push('termination_status') },
}))

vi.mock('../router', () => ({
  isPipelineRelevant: (pid: string) => pid === 'pipe-relevant' || !pid,
  resolvePipelineId: (data: any) => {
    const pid = data?.data?.pipeline_id ?? data?.pipeline_id
    return typeof pid === 'string' && pid.length > 0 ? pid : null
  },
}))

import { WS_SERVER_EVENTS } from '@/constants/websocket'

describe('initStreamingEvents 全局流式事件接线', () => {
  beforeEach(async () => {
    vi.resetModules()
    invoked.calls.length = 0
    mockSubscribe.mockClear()
    mockUnsubscribe.mockClear()
    mockHandlers.clear()
    mockLogger.debug.mockClear()
    mockLogger.info.mockClear()
  })

  afterEach(() => {
    vi.resetModules()
  })

  it('init 幂等：重复调用只订阅一次（_initialized 门控）', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()
    const firstSubscribeCount = mockSubscribe.mock.calls.length
    mod.initStreamingEvents()
    expect(mockSubscribe.mock.calls.length).toBe(firstSubscribeCount)
  })

  it('订阅全部流式事件 + reconnected，且 handler 已注册', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()

    const expectedEvents = [
      WS_SERVER_EVENTS.STREAM_START,
      WS_SERVER_EVENTS.BLOCK_START,
      WS_SERVER_EVENTS.TEXT_DELTA,
      WS_SERVER_EVENTS.REASONING_DELTA,
      WS_SERVER_EVENTS.TOOL_CALL_DELTA,
      WS_SERVER_EVENTS.BLOCK_END,
      WS_SERVER_EVENTS.USAGE_EVENT,
      WS_SERVER_EVENTS.FINISH,
      WS_SERVER_EVENTS.STREAM_END,
      WS_SERVER_EVENTS.STREAM_ERROR,
      WS_SERVER_EVENTS.NEW_MESSAGE,
      WS_SERVER_EVENTS.TOOL_START,
      WS_SERVER_EVENTS.TOOL_RESULT,
      WS_SERVER_EVENTS.TOOL_PROGRESS,
      WS_SERVER_EVENTS.ITERATION,
      WS_SERVER_EVENTS.COST_UPDATE,
      WS_SERVER_EVENTS.TERMINATION_STATUS,
      WS_SERVER_EVENTS.SYSTEM_NOTIFICATION,
      'reconnected',
    ]
    const subscribedEvents = mockSubscribe.mock.calls.map((c) => c[0])
    for (const ev of expectedEvents) {
      expect(subscribedEvents).toContain(ev)
    }
  })

  it('相关 pipeline 的事件透传到业务 handler；非相关 pipeline 被中央门控丢弃', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()

    const fire = (event: string, data: any) => mockHandlers.get(event)!(data)

    fire(WS_SERVER_EVENTS.STREAM_START, { data: { pipeline_id: 'pipe-relevant' } })
    fire(WS_SERVER_EVENTS.STREAM_START, { data: { pipeline_id: 'pipe-other-ignored' } })

    expect(invoked.calls).toEqual(['stream_start'])
    expect(mockLogger.info).toHaveBeenCalledWith(
      expect.stringContaining('drop irrelevant pipeline event'),
    )
  })

  it('无 pipelineId 的会话级/全局事件（pid 为空）照常放行', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()

    mockHandlers.get(WS_SERVER_EVENTS.ITERATION)!({ iteration: 2, max_iterations: 5 })
    mockHandlers.get(WS_SERVER_EVENTS.TERMINATION_STATUS)!({ data: {} })

    expect(invoked.calls).toContain('iteration')
    expect(invoked.calls).toContain('termination_status')
  })

  it('高频增量事件（text/reasoning/tool_call_delta、keepalive）不写事件日志', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()

    mockLogger.debug.mockClear()
    mockHandlers.get(WS_SERVER_EVENTS.TEXT_DELTA)!({ data: { pipeline_id: 'pipe-relevant', message_id: 'm1', content: 'x' } })
    mockHandlers.get(WS_SERVER_EVENTS.REASONING_DELTA)!({ data: { pipeline_id: 'pipe-relevant', message_id: 'm1', content: 'y' } })
    mockHandlers.get(WS_SERVER_EVENTS.TOOL_CALL_DELTA)!({ data: { pipeline_id: 'pipe-relevant', message_id: 'm1' } })

    const logCalls = mockLogger.debug.mock.calls.filter((c) => String(c[0]).includes('[WS-EVENT]'))
    expect(logCalls.length).toBe(0)
  })

  it('普通事件记录 [WS-EVENT] 调试日志（含 pid/mid/contentLen）', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()

    mockLogger.debug.mockClear()
    mockHandlers.get(WS_SERVER_EVENTS.STREAM_END)!({
      pipeline_id: 'pipe-relevant-1234567890ab',
      data: { message_id: 'msg-abcdef123456', content: '你好' },
    })

    const logCalls = mockLogger.debug.mock.calls.filter((c) => String(c[0]).includes('[WS-EVENT]'))
    expect(logCalls.length).toBe(1)
    const line = String(logCalls[0][0])
    expect(line).toContain('stream_end')
    expect(line).toContain('pipe-relevan') // pid 截断前 12 字符
    expect(line).toContain('msg-abcdef')
    expect(line).toContain('contentLen=2')
  })

  it('destroy 反注册全部订阅并清空 handler 表；再次 destroy 幂等', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()
    const subscribedCount = mockSubscribe.mock.calls.length

    mod.destroyStreamingEvents()
    expect(mockUnsubscribe.mock.calls.length).toBe(subscribedCount)
    expect(mockHandlers.size).toBe(0)
    // 幂等：再次 destroy 不产生新反注册
    const unsubCount = mockUnsubscribe.mock.calls.length
    mod.destroyStreamingEvents()
    expect(mockUnsubscribe.mock.calls.length).toBe(unsubCount)
  })

  it('destroy 后可再次 init（重新订阅）', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()
    mod.destroyStreamingEvents()
    const unsubCount = mockUnsubscribe.mock.calls.length

    mod.initStreamingEvents()
    expect(mockSubscribe.mock.calls.length).toBeGreaterThan(0)
    expect(mockUnsubscribe.mock.calls.length).toBe(unsubCount) // init 不清 handler
    // 事件能再次透传
    mockHandlers.get(WS_SERVER_EVENTS.STREAM_START)!({ data: { pipeline_id: 'pipe-relevant' } })
    expect(invoked.calls).toEqual(['stream_start'])
  })

  it('reinit = destroy + init（订阅重新建立，事件再次可到达）', async () => {
    const mod = await import('../index')
    mod.initStreamingEvents()
    mockUnsubscribe.mockClear()
    mockSubscribe.mockClear()

    mod.reinitStreamingEvents()
    expect(mockUnsubscribe.mock.calls.length).toBeGreaterThan(0)
    expect(mockSubscribe.mock.calls.length).toBeGreaterThan(0)

    mockHandlers.get(WS_SERVER_EVENTS.STREAM_END)!({ data: { pipeline_id: 'pipe-relevant', message_id: 'm9' } })
    expect(invoked.calls).toContain('stream_end')
  })
})
