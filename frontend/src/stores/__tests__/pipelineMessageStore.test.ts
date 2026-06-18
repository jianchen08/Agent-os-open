/**
 * pipelineMessageStore 测试 - 消息去重、状态同步、initFromAPI 合并
 *
 * 验证：
 * - addMessage 正确插入消息
 * - updateMessage 更新指定消息
 * - initFromAPI 合并数据库消息，保留流式中间态
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

describe('pipelineMessageStore', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  let _seq = 0
  const makeMsg = (id: string, content = '', overrides: Partial<Message> = {}): Message => ({
    id,
    sessionId: 'sess-1',
    sequence: ++_seq,
    role: 'assistant',
    content,
    timestamp: new Date(Date.now() + _seq * 1000).toISOString(),
    ...overrides,
  })

  beforeEach(async () => {
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

  describe('addMessage', () => {
    it('插入新消息到指定 pipeline', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'hello'))

      const msgs = store.getMessages('pipe-1')
      expect(msgs).toHaveLength(1)
      expect(msgs[0].content).toBe('hello')
    })

    it('多条消息共存', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'hello', { sequence: 1 }))
      store.addMessage('pipe-1', makeMsg('msg-2', 'world', { sequence: 2 }))

      const msgs = store.getMessages('pipe-1')
      expect(msgs).toHaveLength(2)
    })

    it('不同 pipeline 消息互不干扰', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'hello'))
      store.addMessage('pipe-2', makeMsg('msg-2', 'world'))

      expect(store.getMessages('pipe-1')).toHaveLength(1)
      expect(store.getMessages('pipe-2')).toHaveLength(1)
    })
  })

  describe('updateMessage', () => {
    it('更新指定消息的部分字段', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'hello'))

      store.updateMessage('pipe-1', 'msg-1', { content: 'updated' })

      const msgs = store.getMessages('pipe-1')
      expect(msgs[0].content).toBe('updated')
    })

    it('更新不存在的消息不报错（静默忽略）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'hello'))

      expect(() => store.updateMessage('pipe-1', 'nonexist', { content: 'x' })).not.toThrow()
    })
  })

  describe('initFromAPI', () => {
    it('初始化空 pipeline 的消息', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-1', [makeMsg('msg-1', 'hello'), makeMsg('msg-2', 'world')])

      expect(store.getMessages('pipe-1')).toHaveLength(2)
    })

    it('合并时保留正在流式的消息（streaming 未被 API 覆盖）', () => {
      const store = usePipelineMessageStore.getState()
      // 模拟流式中消息
      store.addMessage('pipe-1', makeMsg('msg-stream', 'partial...', { status: 'streaming' }))

      // API 返回的消息不包含流式消息
      store.initFromAPI('pipe-1', [makeMsg('msg-1', 'completed')])

      const msgs = store.getMessages('pipe-1')
      const streamMsg = msgs.find(m => m.id === 'msg-stream')
      expect(streamMsg).toBeDefined()
      expect(streamMsg!.status).toBe('streaming')
    })

    it('API 版本替换同 ID 已完成消息', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'partial', { status: 'streaming' }))

      store.initFromAPI('pipe-1', [makeMsg('msg-1', 'full content', { status: 'completed' })])

      const msgs = store.getMessages('pipe-1')
      expect(msgs).toHaveLength(1)
      expect(msgs[0].content).toBe('full content')
    })
  })

  describe('getMessages', () => {
    it('未初始化的 pipeline 返回空数组', () => {
      const store = usePipelineMessageStore.getState()
      expect(store.getMessages('nonexist')).toEqual([])
    })
  })

  describe('isInitialized', () => {
    it('未加载消息时返回 false', () => {
      expect(usePipelineMessageStore.getState().isInitialized('pipe-1')).toBe(false)
    })

    it('initFromAPI 后返回 true', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-1', [])
      expect(store.isInitialized('pipe-1')).toBe(true)
    })
  })
})
