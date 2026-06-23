/**
 * 复现测试：persist 写入 localStorage 超配额时的 store 行为
 *
 * Bug 场景：
 * - localStorage 配额已满，persist 的 setItem 抛 QuotaExceededError
 * - 期望：内存 state 仍正常更新，业务（addMessage/initFromAPI）不抛异常
 * - 实际（修复前）：异常冒泡到调用方，fetchMessages 误判为"加载失败"
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = '204ecb54c76e000000000000'
const SESSION_ID = 'sess-quota-test'

describe('persist 超配额时 store 行为', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let originalSetItem: typeof Storage.prototype.setItem

  const makeMsg = (id: string, seq: number): Message => ({
    id,
    sessionId: SESSION_ID,
    sequence: seq,
    role: 'assistant',
    content: `reply ${seq}`,
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
  })

  beforeEach(async () => {
    // 让所有 localStorage.setItem 都抛 QuotaExceededError，模拟配额已满
    originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = vi.fn(() => {
      const err = new DOMException(
        "Failed to execute 'setItem' on 'Storage': Setting the value of 'pipeline-messages' exceeded the quota.",
        'QuotaExceededError',
      )
      throw err
    })

    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: {},
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })
  })

  afterEach(() => {
    Storage.prototype.setItem = originalSetItem
  })

  it('addMessage 在 persist 失败时不应抛异常，且内存 state 应更新', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID } as any)

    // 不应抛出 QuotaExceededError
    expect(() => {
      store.addMessage(PIPELINE_ID, makeMsg('m1', 1))
    }).not.toThrow()

    // 内存 state 必须更新（persist 失败不能影响业务）
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(1)
    expect(msgs[0].content).toBe('reply 1')
  })

  it('initFromAPI 在 persist 失败时不应抛异常，且内存 state 应更新', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID } as any)

    expect(() => {
      store.initFromAPI(PIPELINE_ID, [
        makeMsg('m1', 1),
        makeMsg('m2', 2),
      ])
    }).not.toThrow()

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(2)
  })

  it('连续多次 addMessage（每次触发 persist）在配额满时都应成功', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID } as any)

    for (let i = 1; i <= 10; i++) {
      expect(() => store.addMessage(PIPELINE_ID, makeMsg(`m${i}`, i))).not.toThrow()
    }

    expect(store.getMessages(PIPELINE_ID)).toHaveLength(10)
  })
})
