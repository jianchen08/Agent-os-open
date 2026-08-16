/** pipelineMessageStore 测试 - 消息去重、状态同步、initFromAPI 合并 验证： */
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
      // 用 user/assistant 不同 role 避免被 mergeConsecutiveAssistantMessages 合并
      store.initFromAPI('pipe-1', [
        makeMsg('msg-1', 'hello', { role: 'user' }),
        makeMsg('msg-2', 'world', { role: 'assistant' }),
      ])

      expect(store.getMessages('pipe-1')).toHaveLength(2)
    })

    it('刷新时丢弃本地 streaming 占位（initFromAPI 全量替换，不保留本地）', () => {
      const store = usePipelineMessageStore.getState()
      // 模拟流式中消息（API 未返回）
      store.addMessage('pipe-1', makeMsg('msg-stream', 'partial...', { status: 'streaming' }))

      // API 返回的消息不包含流式消息
      store.initFromAPI('pipe-1', [makeMsg('msg-1', 'completed')])

      const msgs = store.getMessages('pipe-1')
      // 现行契约：刷新语义为全量权威替换，本地 streaming 缓存一律丢弃
      // （后端仍在输出时由 WS 重连 backfill + 续流补回）
      expect(msgs.find(m => m.id === 'msg-stream')).toBeUndefined()
      expect(msgs).toHaveLength(1)
      expect(msgs[0].id).toBe('msg-1')
    })

    it('API 版本替换同 ID 已完成消息', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('msg-1', 'partial', { status: 'streaming' }))

      store.initFromAPI('pipe-1', [makeMsg('msg-1', 'full content', { status: 'completed' })])

      const msgs = store.getMessages('pipe-1')
      expect(msgs).toHaveLength(1)
      expect(msgs[0].content).toBe('full content')
    })

    // 注意: initFromAPI 现为「全量替换」语义：本地消息一律丢弃，只保留 API 权威数据。
    // 乱序 API 消息按 sequence 升序排序。
    it('刷新后 localOnly 消息被丢弃（全量替换），API 消息按 sequence 升序', () => {
      const store = usePipelineMessageStore.getState()
      // 模拟 persist 恢复的旧消息（sequence=1，本地独有，API 未返回）
      store.addMessage('pipe-1', makeMsg('old-msg', 'old content', { sequence: 1, role: 'user', status: 'completed' }))
      // API 返回最近的新消息（sequence 10、20、30，不含旧消息）
      store.initFromAPI('pipe-1', [
        makeMsg('api-10', 'msg10', { sequence: 10, role: 'assistant', status: 'completed' }),
        makeMsg('api-20', 'msg20', { sequence: 20, role: 'user', status: 'completed' }),
        makeMsg('api-30', 'msg30', { sequence: 30, role: 'assistant', status: 'completed' }),
      ])

      const msgs = store.getMessages('pipe-1')
      // 现行契约：本地 localOnly 不合并、不保留，只剩 API 的 3 条
      expect(msgs).toHaveLength(3)
      // API 消息按 sequence 升序
      expect(msgs.map(m => m.sequence)).toEqual([10, 20, 30])
      expect(msgs[0].id).toBe('api-10')
      expect(msgs[2].id).toBe('api-30')
    })

    it('API 消息乱序时按 sequence 升序正确排序', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('local-5', 'c5', { sequence: 5, role: 'user', status: 'completed' }))
      store.addMessage('pipe-1', makeMsg('local-2', 'c2', { sequence: 2, role: 'user', status: 'completed' }))
      store.initFromAPI('pipe-1', [
        makeMsg('api-8', 'c8', { sequence: 8, role: 'assistant', status: 'completed' }),
        makeMsg('api-3', 'c3', { sequence: 3, role: 'assistant', status: 'completed' }),
      ])

      const msgs = store.getMessages('pipe-1')
      // 现行契约：全量替换丢弃 local（5、2），API 消息按 sequence 升序
      expect(msgs.map(m => m.sequence)).toEqual([3, 8])
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

    it('空数组 initFromAPI 后仍返回 false（空初始化不算已加载，count<=1）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-1', [])
      // 空数组：count=0、bottomCursor=0 → 未初始化，下次应走全量而非增量补漏
      expect(store.isInitialized('pipe-1')).toBe(false)
    })

    it('加载多条消息（bottomCursor>0 且 count>1）后返回 true', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-1', [
        { id: 'm1', role: 'user', content: 'q', sequence: 1, timestamp: '2026-01-01T00:00:00Z' } as any,
        { id: 'm2', role: 'assistant', content: 'a', sequence: 2, timestamp: '2026-01-01T00:00:01Z' } as any,
      ])
      expect(store.isInitialized('pipe-1')).toBe(true)
    })
  })
})
