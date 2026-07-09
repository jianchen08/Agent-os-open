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
  // 导出真实的 mergeConsecutiveAssistantMessages（纯函数，无需 mock）
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
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
    it('initFromAPI 完全丢弃本地消息，只用 API 权威数据（不保留 streaming）', () => {
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

      // 5. 新语义：刷新 = 全量替换，本地 streaming 消息被丢弃，仅保留 API 权威数据。
      //    流式恢复由 WS 重连 backfill 处理，不再依赖本地保留。
      const msgsAfterInit = store.getMessages(PIPELINE_ID)
      const streamingMsg = msgsAfterInit.find((m) => m.id === MESSAGE_ID)
      expect(streamingMsg).toBeUndefined()
      expect(msgsAfterInit).toHaveLength(2)
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

  describe('场景4: tool_start/tool_result 后续 chunks 无多余 stream_start', () => {
    it('工具调用后直接追加 chunks，消息仍可找到', () => {
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
      // tool_result 后不再发多余的 stream_start，直接追加后续 chunks
      store.stopStreaming(PIPELINE_ID)
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      store.finalizeMessage(PIPELINE_ID, MESSAGE_ID)

      // 4. 最终 stream_end（无多余 stream_start）
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      store.finalizeMessage(PIPELINE_ID, MESSAGE_ID)

      const msg = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(msg).toBeDefined()
      expect(msg!.status).toBe('completed')
    })
  })

  describe('场景5: fetchMessages (initFromAPI) 覆盖场景', () => {
    it('initFromAPI 完全丢弃本地消息，WS 新消息（含 streaming）不再保留', () => {
      const store = usePipelineMessageStore.getState()

      // 1. 加载历史（使用固定 sequence 模拟真实 API 数据）
      store.initFromAPI(PIPELINE_ID, [
        { id: 'old-1', sessionId: SESSION_ID, role: 'user', content: 'history', sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as Message,
      ])

      // 2. 用户发消息（前端创建，sequence=2）
      store.addMessage(PIPELINE_ID, { id: 'user-1', sessionId: SESSION_ID, role: 'user', content: 'test', sequence: 2, timestamp: new Date().toISOString(), parentId: null } as Message)

      // 3. stream_start: ensureStreamingPlaceholder 计算 nextSeq
      const existingMsgs = store.getMessages(PIPELINE_ID)
      const nextSeq = existingMsgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
      expect(nextSeq).toBe(3)

      store.startStreaming(PIPELINE_ID, MESSAGE_ID)
      store.addMessage(PIPELINE_ID, {
        id: MESSAGE_ID,
        sessionId: SESSION_ID,
        role: 'assistant',
        content: '',
        sequence: nextSeq,
        timestamp: new Date().toISOString(),
        parentId: null,
        status: 'streaming',
      } as Message)

      expect(store.getMessages(PIPELINE_ID)).toHaveLength(3)

      // 4. 模拟另一个 fetchMessages → initFromAPI（API 还没有 streaming 消息）
      store.initFromAPI(PIPELINE_ID, [
        { id: 'old-1', sessionId: SESSION_ID, role: 'user', content: 'history', sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as Message,
        { id: 'user-1', sessionId: SESSION_ID, role: 'user', content: 'test', sequence: 2, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as Message,
      ])

      const msgs = store.getMessages(PIPELINE_ID)

      // 5. 新语义：刷新 = 全量替换，本地 streaming 消息被丢弃，仅保留 API 权威数据。
      const streamingMsg = msgs.find((m) => m.id === MESSAGE_ID)
      expect(streamingMsg).toBeUndefined()
      // 本地乐观 user-1（sequence=2）虽与 API 同 id，但全量替换后只看 API，
      // API 恰好含同 id 的 user-1，故最终列表 = API 两条。
      expect(msgs).toHaveLength(2)
      expect(msgs.map((m) => m.id).sort()).toEqual(['old-1', 'user-1'])
    })

    it('initFromAPI 完全丢弃本地系统通知，仅保留 API 权威数据', () => {
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

      // 新语义：刷新 = 全量替换，本地 system 消息被丢弃（不再保留为结构边界）。
      // 后端持久化的内容由 API 决定，瞬态系统通知刷新后即消失。
      const msgs = store.getMessages(PIPELINE_ID)
      const systemMsg = msgs.find((m) => m.id === 'ws-msg-1')
      expect(systemMsg).toBeUndefined()
      expect(msgs).toHaveLength(1)
      expect(msgs[0].id).toBe('api-msg-1')
    })
  })

  describe('场景6: 不同 ID 的消息不因 sequence 相同而合并（减法：已移除 sequence+role 模糊去重）', () => {
    it('不同 ID 的 assistant 消息即使 sequence 相同也应保持独立', () => {
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

      // API 返回同 sequence 但不同 ID → 应为独立消息，不合并
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

      // 两条消息应独立存在
      const msgs = store.getMessages(PIPELINE_ID)
      const assistantMsgs = msgs.filter((m) => m.role === 'assistant')
      expect(assistantMsgs).toHaveLength(2)

      // WS 消息应能通过原始 ID 找到
      const wsMsg = msgs.find((m) => m.id === MESSAGE_ID)
      expect(wsMsg).toBeDefined()
      expect(wsMsg!.status).toBe('streaming')

      // API 消息也应独立存在
      const apiMsg = msgs.find((m) => m.id === 'api-different-id')
      expect(apiMsg).toBeDefined()
      expect(apiMsg!.status).toBe('completed')

      // stream_end 用 MESSAGE_ID updateMessage 时必须能找到
      store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
      const afterUpdate = store.getMessages(PIPELINE_ID).find((m) => m.id === MESSAGE_ID)
      expect(afterUpdate).toBeDefined()
      expect(afterUpdate!.status).toBe('completed')
    })
  })
})
