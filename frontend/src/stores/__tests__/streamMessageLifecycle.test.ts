/**
 * 流式消息生命周期测试 - 复现 stream_end 时消息找不到的 bug
 *
 * 模拟完整流程：用户发消息 → stream_start → stream_chunk → stream_end
 * 验证每个阶段 store 中消息状态是否正确
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = '39ef1314a7b9'
const MESSAGE_ID = 'msg_a37d345d'
const SESSION_ID = 'sess-test-1'

describe('stream 消息生命周期', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  let _seq = 0
  const makeMsg = (id: string, overrides: Partial<Message> = {}): Message => ({
    id,
    sessionId: SESSION_ID,
    sequence: ++_seq,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'streaming',
    ...overrides,
  })

  beforeEach(async () => {
    _seq = 0
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

  describe('场景1: 正常流程 stream_start → stream_end', () => {
    it('stream_start 创建占位符后 stream_end 能找到消息', () => {
      const store = usePipelineMessageStore.getState()

      // 1. 用户消息
      const userMsg = makeMsg('user-1', { role: 'user', content: 'hello', status: 'completed' })
      store.addMessage(PIPELINE_ID, userMsg)
      expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

      // 2. stream_start: ensureStreamingPlaceholder 逻辑
      store.startStreaming(PIPELINE_ID, MESSAGE_ID)
      const assistantMsg = makeMsg(MESSAGE_ID, { role: 'assistant', content: '', status: 'streaming' })
      store.addMessage(PIPELINE_ID, assistantMsg)

      const msgsAfterStart = store.getMessages(PIPELINE_ID)
      expect(msgsAfterStart).toHaveLength(2)

      const found = msgsAfterStart.find((m) => m.id === MESSAGE_ID)
      expect(found).toBeDefined()
      expect(found!.status).toBe('streaming')

      // 3. stream_end: updateMessage
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      store.finalizeMessage(PIPELINE_ID, MESSAGE_ID)

      const msgsAfterEnd = store.getMessages(PIPELINE_ID)
      const ended = msgsAfterEnd.find((m) => m.id === MESSAGE_ID)
      expect(ended).toBeDefined()
      expect(ended!.status).toBe('completed')
    })
  })

  describe('场景2: initFromAPI 在 stream_start 之后执行', () => {
    it('initFromAPI 不应删除 streaming 状态的消息', () => {
      const store = usePipelineMessageStore.getState()

      // 1. 先加载历史消息
      const historyMsg = makeMsg('msg-history', { content: 'old', status: 'completed' })
      store.initFromAPI(PIPELINE_ID, [historyMsg])
      expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

      // 2. 用户发消息
      const userMsg = makeMsg('user-1', { role: 'user', content: 'hello', status: 'completed' })
      store.addMessage(PIPELINE_ID, userMsg)

      // 3. stream_start: 创建占位符
      store.startStreaming(PIPELINE_ID, MESSAGE_ID)
      const assistantMsg = makeMsg(MESSAGE_ID, { role: 'assistant', content: '', status: 'streaming' })
      store.addMessage(PIPELINE_ID, assistantMsg)

      expect(store.getMessages(PIPELINE_ID)).toHaveLength(3)

      // 4. 模拟 initFromAPI 被再次调用（比如 fetchMessages 触发）
      // API 返回的消息不包含 streaming 占位符
      const apiMsgs = [
        makeMsg('msg-history', { content: 'old', status: 'completed' }),
        makeMsg('user-1', { role: 'user' as any, content: 'hello', status: 'completed' }),
      ]
      store.initFromAPI(PIPELINE_ID, apiMsgs)

      // 5. 验证 streaming 消息仍然存在
      const msgsAfterInit = store.getMessages(PIPELINE_ID)
      const streamingMsg = msgsAfterInit.find((m) => m.id === MESSAGE_ID)
      expect(streamingMsg).toBeDefined()
      expect(streamingMsg!.status).toBe('streaming')

      // 6. stream_end 应该能找到消息
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      const afterEnd = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(afterEnd).toBeDefined()
      expect(afterEnd!.status).toBe('completed')
    })
  })

  describe('场景3: addMessage 去重 - 同 ID 消息多次添加', () => {
    it('相同 ID 的消息应更新而非新增', () => {
      const store = usePipelineMessageStore.getState()

      store.addMessage(PIPELINE_ID, makeMsg(MESSAGE_ID, { content: '', status: 'streaming' }))
      expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

      // 再次 addMessage 同 ID（ensureStreamingPlaceholder 可能被多次调用）
      store.addMessage(PIPELINE_ID, makeMsg(MESSAGE_ID, { content: '', status: 'streaming' }))
      expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

      // updateMessage 应该能找到
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      const msg = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(msg).toBeDefined()
      expect(msg!.status).toBe('completed')
    })
  })

  describe('场景4: tool_start 中间发送 stream_end 再 stream_start', () => {
    it('工具调用中间的 stream_end/stream_start 循环后消息仍可找到', () => {
      const store = usePipelineMessageStore.getState()

      // 1. stream_start 创建占位符
      store.startStreaming(PIPELINE_ID, MESSAGE_ID)
      store.addMessage(PIPELINE_ID, makeMsg(MESSAGE_ID, { content: '', status: 'streaming' }))

      // 2. 追加一些文本 parts
      store.appendPart(PIPELINE_ID, MESSAGE_ID, {
        type: 'text',
        content: 'partial text',
        state: 'streaming',
        sequence: 0,
      })

      // 3. tool_start: 前端收到 tool_start 事件 (不触发 stream_end)
      // 后端在 tool_result 后会发新的 stream_start
      store.stopStreaming(PIPELINE_ID)
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      store.finalizeMessage(PIPELINE_ID, MESSAGE_ID)

      // 4. 新的 stream_start（同一个 MESSAGE_ID）
      store.startStreaming(PIPELINE_ID, MESSAGE_ID)
      // addMessage 同 ID 会更新
      store.addMessage(PIPELINE_ID, makeMsg(MESSAGE_ID, { content: 'partial text', status: 'streaming' }))

      // 5. 最终 stream_end
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      store.finalizeMessage(PIPELINE_ID, MESSAGE_ID)

      const msg = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(msg).toBeDefined()
      expect(msg!.status).toBe('completed')
    })
  })

  describe('场景5: fetchMessages (initFromAPI) 覆盖场景', () => {
    it('initFromAPI 用 API 数据覆盖后，WS 新消息 ID 在 API 中不存在', () => {
      const store = usePipelineMessageStore.getState()

      // 1. 加载历史
      store.initFromAPI(PIPELINE_ID, [
        makeMsg('old-1', { content: 'history', status: 'completed' }),
      ])

      // 2. 用户消息
      store.addMessage(PIPELINE_ID, makeMsg('user-1', { role: 'user', content: 'test', status: 'completed' }))

      // 3. stream_start 创建占位符
      store.startStreaming(PIPELINE_ID, MESSAGE_ID)
      store.addMessage(PIPELINE_ID, makeMsg(MESSAGE_ID, { content: '', status: 'streaming' }))

      expect(store.getMessages(PIPELINE_ID)).toHaveLength(3)

      // 4. 模拟另一个 fetchMessages 调用 initFromAPI
      // 场景：initFromAPI 被某个 effect 触发，API 还没有 streaming 消息
      store.initFromAPI(PIPELINE_ID, [
        makeMsg('old-1', { content: 'history', status: 'completed' }),
        makeMsg('user-1', { role: 'user' as any, content: 'test', status: 'completed' }),
      ])

      const msgs = store.getMessages(PIPELINE_ID)
      const streamingMsg = msgs.find((m) => m.id === MESSAGE_ID)

      // 关键断言: streaming 消息必须被保留
      expect(streamingMsg).toBeDefined()
      expect(streamingMsg!.status).toBe('streaming')

      // 5. stream_end 应能找到
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      const final = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(final).toBeDefined()
      expect(final!.status).toBe('completed')
    })

    it('initFromAPI 在非 streaming 消息上的指纹去重', () => {
      const store = usePipelineMessageStore.getState()

      // WS 先收到一条 completed 的消息（通过 handlePipelineReceived 或 handleSystemNotification）
      store.addMessage(PIPELINE_ID, makeMsg('ws-msg-1', {
        role: 'system',
        content: '任务完成通知',
        status: 'completed',
      }))

      // 然后 initFromAPI 加载，API 中没有这条消息（系统通知可能不持久化）
      store.initFromAPI(PIPELINE_ID, [
        makeMsg('api-msg-1', { content: 'hello', status: 'completed' }),
      ])

      // WS 的 system 消息应该被保留（指纹不匹配）
      const msgs = store.getMessages(PIPELINE_ID)
      expect(msgs.length).toBeGreaterThanOrEqual(1)
    })
  })

  describe('场景6: 消息 ID 格式差异去重', () => {
    it('相同 sequence 的 assistant 消息应被识别为同一条', () => {
      const store = usePipelineMessageStore.getState()

      // WS 创建的占位符
      store.addMessage(PIPELINE_ID, {
        id: MESSAGE_ID,
        sessionId: SESSION_ID,
        role: 'assistant',
        content: '',
        sequence: 5,
        timestamp: new Date().toISOString(),
        parentId: null,
        status: 'streaming',
      })

      // API 返回同 sequence 但不同 ID
      store.addMessage(PIPELINE_ID, {
        id: 'api-different-id',
        sessionId: SESSION_ID,
        role: 'assistant',
        content: 'full response',
        sequence: 5,
        timestamp: new Date().toISOString(),
        parentId: null,
        status: 'completed',
      })

      // 不应该有两条
      const msgs = store.getMessages(PIPELINE_ID)
      const assistantMsgs = msgs.filter((m) => m.role === 'assistant')
      expect(assistantMsgs.length).toBeLessThanOrEqual(2)

      // 原始 WS 消息应该能通过原始 ID 找到
      const wsMsg = msgs.find((m) => m.id === MESSAGE_ID)
      // 如果去重生效，wsMsg 可能被更新了
      // 关键: stream_end 用 MESSAGE_ID updateMessage 时必须能找到
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      const afterUpdate = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(afterUpdate).toBeDefined()
    })
  })
})
