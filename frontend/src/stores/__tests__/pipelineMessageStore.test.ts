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

  describe('claimUserMessage', () => {
    const CMID = '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f'
    const RECORD_ID = 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'

    it('认领乐观 user：UI id 不变 + recordId 独立字段 + 权威 seq 补正', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', {
        id: CMID, sessionId: 'sess-1', role: 'user', content: '你好',
        timestamp: new Date().toISOString(), status: 'sending', clientMessageId: CMID,
      } as Message)
      const result = store.claimUserMessage('pipe-1', CMID, {
        id: RECORD_ID, content: '你好', sequence: 4, metadata: { client_message_id: CMID },
      })
      expect(result).toBe('upgraded')
      const msgs = store.getMessages('pipe-1')
      const user = msgs.find((m) => m.clientMessageId === CMID)
      expect(user).toBeDefined()
      expect(user?.id).toBe(CMID) // UI 寻址 id 永不迁移（React key 稳定）
      expect(user?.recordId).toBe(RECORD_ID) // 权威 id 记入独立字段
      expect(user?.sequence).toBe(4)
      expect(user?.status).toBe('completed')
      expect(msgs).toHaveLength(1) // 单一消息数组：无 pending 区、无重复
    })

    it('重复认领（同 recordId）→ skipped 幂等', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage('pipe-1', makeMsg('m1', '', {
        role: 'user', clientMessageId: CMID, recordId: RECORD_ID, status: 'completed',
      }))
      const result = store.claimUserMessage('pipe-1', CMID, {
        id: RECORD_ID, content: '你好', sequence: 4,
      })
      expect(result).toBe('skipped')
      expect(store.getMessages('pipe-1')).toHaveLength(1)
    })

    it('候选缺失 → inserted 补插权威 user（不丢消息）', () => {
      const store = usePipelineMessageStore.getState()
      const result = store.claimUserMessage('pipe-1', CMID, {
        id: RECORD_ID, content: '你好', sequence: 4,
      })
      expect(result).toBe('inserted')
      const msgs = store.getMessages('pipe-1')
      expect(msgs).toHaveLength(1)
      expect(msgs[0].id).toBe(CMID)
      expect(msgs[0].recordId).toBe(RECORD_ID)
      expect(msgs[0].sequence).toBe(4)
    })
  })

  describe('对账双键收敛（ADR 2026-08-22）', () => {
    const CMID = '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f'
    const RECORD_ID = 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'

    it('initFromAPI：本地已认领 user（id=uuid, recordId=mc_）被 API 权威版（id=mc_）收敛，不产生重复气泡', () => {
      const store = usePipelineMessageStore.getState()
      // 本地：认领后的 user（UI id=uuid）+ assistant
      store.addMessage('pipe-1', makeMsg(CMID, '你好', {
        role: 'user', clientMessageId: CMID, recordId: RECORD_ID, status: 'completed', sequence: 4,
      }))
      store.addMessage('pipe-1', makeMsg('a_old', '回复', { role: 'assistant', sequence: 5 }))
      // API 权威：user id=后端 record_id（= recordId），cmid 一致
      store.initFromAPI('pipe-1', [
        makeMsg(RECORD_ID, '你好', { role: 'user', clientMessageId: CMID, sequence: 4 }),
        makeMsg('a_old', '回复', { role: 'assistant', sequence: 5 }),
      ])
      const msgs = store.getMessages('pipe-1')
      const users = msgs.filter((m) => m.role === 'user')
      expect(users).toHaveLength(1) // 不并存不重复
      expect(msgs.some((m) => m.id === CMID)).toBe(false) // 本地 uuid 版让位 API 权威版
      expect(msgs.some((m) => m.id === RECORD_ID)).toBe(true)
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
