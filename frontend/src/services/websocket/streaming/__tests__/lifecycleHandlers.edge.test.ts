// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * lifecycleHandlers 未覆盖分支补充测试
 *
 * 覆盖（相对既有 lifecycleHandlers.test.ts 的缺口）：
 * 1. handleReconnected：清残留 streaming thinking parts（stuck messages 收尾）
 * 2. handleReconnected：streaming 管道缺 threadId → 计入失败列表
 * 3. handleReconnected：loadPipelineMessages reject → 捕获计入失败 + 弹警告
 * 4. handleCostUpdate：cache_hit_ratio 非有限值 → 不触发骤降检测
 * 5. handleCostUpdate：命中率恢复 ≥70% → 解除 alert 状态，可再次提示
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const { mockStore, mockAddNotification } = vi.hoisted(() => ({
  mockStore: {
    streamingState: {},
    messagesByPipeline: {},
    pipelineSessionMap: {},
    updateMessage: vi.fn(),
    appendPart: vi.fn(),
    loadPipelineMessages: vi.fn(),
  },
  mockAddNotification: vi.fn(),
}))

vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: { getState: () => mockStore },
}))

vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: mockAddNotification }) },
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

vi.mock('../handlers/utils', () => ({
  terminatePipeline: vi.fn(),
}))

/** contextUsageStore mock 用的分桶存储（handleCostUpdate 测试） */
let usageMap: Record<string, any> = {}

import { handleReconnected } from '../lifecycleHandlers'

const STREAMING_PIPELINE = 'pipe-edge-001'
const THREAD_ID = 'thread-edge-001'

describe('handleReconnected - 补充分支', () => {
  beforeEach(() => {
    mockStore.streamingState = {}
    mockStore.messagesByPipeline = {}
    mockStore.pipelineSessionMap = {}
    mockStore.updateMessage.mockClear()
    mockStore.appendPart.mockClear()
    mockStore.loadPipelineMessages.mockReset()
    mockStore.loadPipelineMessages.mockResolvedValue({ ok: true })
    mockAddNotification.mockClear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('清理残留 streaming thinking parts：state=streaming → done', async () => {
    mockStore.streamingState = {}
    mockStore.messagesByPipeline = {
      [STREAMING_PIPELINE]: [
        {
          id: 'msg-stuck',
          role: 'assistant',
          status: 'completed',
          parts: [
            { type: 'thinking', state: 'streaming', content: 'x' },
            { type: 'text', state: 'done', content: 'y' },
          ],
        },
      ],
    }
    // 无 pipelineSessionMap → 该管道进 failed 列表（同时触发 updateMessage 清理）
    await handleReconnected()
    await vi.advanceTimersByTimeAsync(0)

    expect(mockStore.updateMessage).toHaveBeenCalledWith(
      STREAMING_PIPELINE,
      'msg-stuck',
      expect.objectContaining({
        parts: expect.arrayContaining([
          expect.objectContaining({ type: 'thinking', state: 'done' }),
        ]),
      }),
    )
  })

  it('streaming 管道缺 threadId → 计入失败列表并弹「可能丢失」警告', async () => {
    mockStore.streamingState = {
      [STREAMING_PIPELINE]: { isStreaming: true, messageId: 'msg-1' },
    }
    mockStore.messagesByPipeline = {}
    mockStore.pipelineSessionMap = {} // threadId 缺失

    await handleReconnected()
    await vi.advanceTimersByTimeAsync(20000)

    expect(mockStore.loadPipelineMessages).not.toHaveBeenCalled()
    expect(mockAddNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        title: '流式消息可能丢失',
        message: expect.stringContaining('1 个流式管道可能丢失消息'),
      }),
    )
  })

  it('loadPipelineMessages reject → 捕获（warn）计入失败并弹警告', async () => {
    mockStore.streamingState = {
      [STREAMING_PIPELINE]: { isStreaming: true, messageId: 'msg-2' },
    }
    mockStore.messagesByPipeline = {}
    mockStore.pipelineSessionMap = { [STREAMING_PIPELINE]: THREAD_ID }
    mockStore.loadPipelineMessages.mockRejectedValue(new Error('network down'))

    await handleReconnected()
    await vi.advanceTimersByTimeAsync(20000)

    expect(mockStore.loadPipelineMessages).toHaveBeenCalled()
    expect(mockAddNotification).toHaveBeenCalledWith(
      expect.objectContaining({ title: '流式消息可能丢失' }),
    )
  })
})

describe('handleCostUpdate - cache 骤降检测边界', () => {
  beforeEach(async () => {
    vi.resetModules()
    // 重新装载真实 store mock（本文件顶部 mock 已生效，这里补 usage store）
    vi.doMock('@/stores/contextUsageStore', () => ({
      useContextUsageStore: {
        getState: () => ({
          getUsage: (pid: string) => usageMap[pid],
          updateUsage: (pid: string, u: any) => {
            usageMap[pid] = { ...usageMap[pid], ...u, hitRatio: u.cache_hit_ratio }
          },
        }),
      },
    }))
    usageMap = {}
    mockAddNotification.mockClear()
  })

  it('cache_hit_ratio 非有限值（NaN）→ 不触发骤降检测不弹通知', async () => {
    const { handleCostUpdate: hcu } = await import('../lifecycleHandlers')
    const { useContextUsageStore: usageStore } = await import('@/stores/contextUsageStore')
    // prevUsage.hitRatio = NaN（历史数据缺失/脏数据）
    usageStore.getState().updateUsage('pipe-c1', { total_tokens: 100, cache_hit_ratio: NaN })
    hcu({ data: { pipeline_id: 'pipe-c1', total_tokens: 200, cache_hit_ratio: 0.2 } })
    expect(mockAddNotification).not.toHaveBeenCalled()
  })

  it('命中率恢复 ≥70% → 解除 alert，再次骤降可再次提示', async () => {
    const { handleCostUpdate: hcu } = await import('../lifecycleHandlers')
    const { useContextUsageStore: usageStore } = await import('@/stores/contextUsageStore')
    // 第一次骤降 0.95 → 0.5（触发一次通知）
    usageStore.getState().updateUsage('pipe-c2', { total_tokens: 10, cache_hit_ratio: 0.95 })
    hcu({ data: { pipeline_id: 'pipe-c2', total_tokens: 20, cache_hit_ratio: 0.5 } })
    expect(mockAddNotification).toHaveBeenCalledTimes(1)

    mockAddNotification.mockClear()
    // 恢复 0.8 → 解除 alert
    hcu({ data: { pipeline_id: 'pipe-c2', total_tokens: 30, cache_hit_ratio: 0.8 } })
    expect(mockAddNotification).not.toHaveBeenCalled()

    // 再次骤降 0.8 → 0.4（解除后重新可提示）
    hcu({ data: { pipeline_id: 'pipe-c2', total_tokens: 40, cache_hit_ratio: 0.4 } })
    expect(mockAddNotification).toHaveBeenCalledTimes(1)
    expect(mockAddNotification).toHaveBeenCalledWith(
      expect.objectContaining({ title: '缓存命中率骤降' }),
    )
  })
})
