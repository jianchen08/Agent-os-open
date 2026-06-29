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
      // 用 user/assistant 不同 role 避免被 mergeConsecutiveAssistantMessages 合并
      store.initFromAPI('pipe-1', [
        makeMsg('msg-1', 'hello', { role: 'user' }),
        makeMsg('msg-2', 'world', { role: 'assistant' }),
      ])

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

    // BUG-FIX-fix_20260623_local_completed_msg_orphan + fix_20260627_message_order_jump_top:
    // persist 恢复的旧 completed 消息（API 未返回）既不是 streaming，也不在 optimistic grace
    // 窗口内（user 无 clientMessageId、assistant 无 _lastUpdated）→ 在 localOnly 过滤时被丢弃，
    // 以 API 权威数据为准。localOnly 只保留 streaming 占位 / grace 消息，其 sequence 不可靠
    // （前端自算 localMax+1），故保持到达顺序追加而非按 sequence 归并。
    // 这两条测试原断言「残留保留并按 sequence 升序合并」，与上述设计冲突，已更新为正确行为。
    it('刷新后 persist 残留的 completed 旧消息被丢弃，以 API 为准', () => {
      const store = usePipelineMessageStore.getState()
      // persist 残留：completed user 消息（无 clientMessageId → 不在 grace 窗口）
      store.addMessage('pipe-1', makeMsg('old-msg', 'old content', { sequence: 1, role: 'user', status: 'completed' }))
      // API 返回最近的新消息（不含残留）
      store.initFromAPI('pipe-1', [
        makeMsg('api-10', 'msg10', { sequence: 10, role: 'assistant', status: 'completed' }),
        makeMsg('api-20', 'msg20', { sequence: 20, role: 'user', status: 'completed' }),
        makeMsg('api-30', 'msg30', { sequence: 30, role: 'assistant', status: 'completed' }),
      ])

      const msgs = store.getMessages('pipe-1')
      // 残留被丢弃，只剩 API 权威 3 条，按 sequence 升序
      expect(msgs.map(m => m.id)).toEqual(['api-10', 'api-20', 'api-30'])
    })

    it('persist 残留多条消息均被丢弃，API 数据按 sequence 升序', () => {
      const store = usePipelineMessageStore.getState()
      // 多条 persist 残留（completed，不在 grace 窗口）
      store.addMessage('pipe-1', makeMsg('local-5', 'c5', { sequence: 5, role: 'user', status: 'completed' }))
      store.addMessage('pipe-1', makeMsg('local-2', 'c2', { sequence: 2, role: 'user', status: 'completed' }))
      // API 用 user/assistant 交替避免被 mergeConsecutiveAssistantMessages 合并
      store.initFromAPI('pipe-1', [
        makeMsg('api-3', 'c3', { sequence: 3, role: 'user', status: 'completed' }),
        makeMsg('api-8', 'c8', { sequence: 8, role: 'assistant', status: 'completed' }),
      ])

      const msgs = store.getMessages('pipe-1')
      // 残留丢弃，只剩 API 2 条按 sequence 升序
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
