/**
 * 复现测试：persist 写入失败时的 store 行为（业务不被阻断）
 *
 * 迁移说明：消息缓存已从 localStorage 迁移到 IndexedDB（见 indexedDbStorage）。
 * 本测试验证的不变量不变——持久化失败（无论是 localStorage 配额满，
 * 还是 IndexedDB 不可用降级为内存）时，业务（addMessage/initFromAPI）不抛异常、
 * 内存 state 正常更新。
 *
 * jsdom 无 IndexedDB，pipelineMessageStore 会自动降级内存模式（见 indexedDbStorage 的
 * safeSet/safeGet），因此下列断言走的是「内存降级」路径，仍能验证业务不变性。
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
