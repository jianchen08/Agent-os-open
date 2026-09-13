// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * pipelineMessageStore 分支缺口补测（branches 冲刺批九）：
 * 内存封顶裁剪、空白消息过滤的 thinking 空内容分支、后台对账三态（重入跳过/
 * 流式跳过/失败重拉）、fetchMessages 错误分级（404/网络码/threadId 缺失）、
 * 同管道取消的拒绝收场、transient 中间态重建占位的防重、注入消息落位矩阵、
 * claim 幂等与无 seq 认领、游标/查询方法的缺省面、Parts 方法的负面路径、
 * persist merge/partialize 与 onRehydrateStorage 清理、水合等待超时兜底。
 *
 * mock 边界仅限网络（apiClient）与持久化存储（indexedDbStorage）；断言输入→输出。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

const { storageGet, storageSet, storageRemove } = vi.hoisted(() => ({
  storageGet: vi.fn<() => Promise<unknown>>(),
  storageSet: vi.fn<() => Promise<void>>(),
  storageRemove: vi.fn<() => Promise<void>>(),
}))
vi.mock('@/utils/indexedDbStorage', () => ({
  indexedDbStorage: {
    getItem: storageGet,
    setItem: storageSet,
    removeItem: storageRemove,
  },
}))

const PIPE = 'pipe-gap-001'
const THREAD = 'thread-gap-001'

function makeMsg(id: string, seq?: number, overrides: Partial<Message> = {}): Message {
  return {
    id,
    sessionId: THREAD,
    ...(seq !== undefined ? { sequence: seq } : {}),
    role: 'assistant',
    content: 'c',
    timestamp: new Date(Date.now() + (seq ?? 0) * 1000).toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

function setApiRecords(records: Record<string, unknown>[], hasMore = false, extra: Record<string, unknown> = {}) {
  mockGet.mockResolvedValueOnce({
    data: { messages: records, total: records.length, has_more: hasMore, ...extra },
  })
}

describe('pipelineMessageStore 分支缺口', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore
  let log: { debug: ReturnType<typeof vi.fn>; info: ReturnType<typeof vi.fn>; warn: ReturnType<typeof vi.fn>; error: ReturnType<typeof vi.fn> }

  beforeEach(async () => {
    vi.clearAllMocks()
    mockGet.mockReset()
    vi.resetModules()
    storageGet.mockReset()
    // 默认水合信号永不完成：由 beforeEach 末尾的手动 rehydrate 显式点亮
    storageGet.mockImplementation(() => new Promise<void>(() => {}))
    storageSet.mockResolvedValue(undefined)
    storageRemove.mockResolvedValue(undefined)
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    log = (await import('@/utils/logger')).loggers.sessionStore as never
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPE]: THREAD },
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
    // 显式完成一次 rehydrate：_hydrated 点亮，后续 auto/backfill 加载不再等 500ms 兜底
    storageGet.mockResolvedValueOnce(null)
    await usePipelineMessageStore.persist.rehydrate()
  })

  describe('内存封顶与空白过滤', () => {
    it('initFromAPI 超 2000 条：按序裁剪保留最新 2000 条（31）', () => {
      const store = usePipelineMessageStore.getState()
      const many = Array.from({ length: 2010 }, (_, i) =>
        makeMsg(`m${i}`, i + 1, { role: i % 2 ? 'assistant' : 'user' }),
      )
      store.initFromAPI(PIPE, many)
      const msgs = store.getMessages(PIPE)
      expect(msgs).toHaveLength(2000)
      expect(msgs[0].sequence).toBe(11)
      expect(msgs[1999].sequence).toBe(2010)
      expect(store.getBottomCursor(PIPE)).toBe(2010)
    })

    it('assistant 仅空白 thinking（无 content/parts）被当空气泡丢弃，有 thinking 内容的保留（277 双面）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-blank', [
        makeMsg('blank-thinking', 1, { content: '', thinking: { content: '   ' } as Message['thinking'] }),
        makeMsg('real-thinking', 2, { content: '', thinking: { content: '思考' } as Message['thinking'] }),
      ])
      const ids = store.getMessages('pipe-blank').map((m) => m.id)
      expect(ids).toEqual(['real-thinking'])
    })
  })

  describe('后台对账（reconcileFromAPI）', () => {
    it('流式进行中：对账静默跳过，不发起请求（58）', async () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('u1', 1, { role: 'user', content: 'q' })])
      store.startStreaming(PIPE, 'streaming-1')
      mockGet.mockClear()
      const result = await store.loadPipelineMessages(PIPE, { threadId: THREAD })
      expect(result.ok).toBe(true)
      expect(mockGet).not.toHaveBeenCalled()
      expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPE]).toBeFalsy()
    })

    it('同一管道对账进行中重入：第二次被去重集合拦下（56）', async () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('u1', 1, { role: 'user', content: 'q' })])
      let resolveApi!: (v: unknown) => void
      mockGet.mockImplementation(() => new Promise((resolve) => { resolveApi = resolve }))
      void store.loadPipelineMessages(PIPE, { threadId: THREAD })
      await Promise.resolve()
      const second = await store.loadPipelineMessages(PIPE, { threadId: THREAD })
      expect(second.ok).toBe(true)
      expect(mockGet).toHaveBeenCalledTimes(1)
      resolveApi({
        data: {
          messages: [
            { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
            { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
          ],
          total: 2,
          has_more: false,
        },
      })
      await vi.waitFor(() => {
        expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPE]).toBe(true)
      })
    })

    it('对账首次失败（错误码形态）→ warn 后强制重拉成功（70）', async () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('u1', 1, { role: 'user', content: 'q' })])
      mockGet.mockRejectedValueOnce(Object.assign(new Error('boom'), { code: 'ERR_OP' }))
      setApiRecords([
        { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
        { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
      ])
      await store.loadPipelineMessages(PIPE, { threadId: THREAD })
      await vi.waitFor(() => {
        expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPE]).toBe(true)
      })
      expect(mockGet).toHaveBeenCalledTimes(2)
      expect(log.warn).toHaveBeenCalledWith(
        expect.stringContaining('reconcileFromAPI'),
        PIPE,
        'ERR_OP',
      )
    })

    it('对账重拉仍失败：保留缓存现状、标记不置位、不向上抛（78-80）', async () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('u1', 1, { role: 'user', content: 'q' })])
      // 首败用非 Error 值：覆盖 errInfo 的 String(error) 兜底侧
      mockGet.mockRejectedValueOnce('plain-fail')
      mockGet.mockRejectedValue(new Error('down'))
      const result = await store.loadPipelineMessages(PIPE, { threadId: THREAD })
      expect(result.ok).toBe(true)
      await vi.waitFor(() => {
        expect(mockGet).toHaveBeenCalledTimes(2)
      })
      expect(log.warn).toHaveBeenCalledWith(
        expect.stringContaining('reconcileFromAPI'),
        PIPE,
        'plain-fail',
      )
      expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPE]).toBeFalsy()
      expect(store.getMessages(PIPE)).toHaveLength(1)
    })
  })

  describe('fetchMessages 错误分级与取消语义', () => {
    it('无 threadId 且无归属映射：报错上抛（1056-1059）', async () => {
      const store = usePipelineMessageStore.getState()
      await expect(store.fetchMessages('pipe-no-map')).rejects.toThrow('无法确定 threadId')
      expect(log.error).toHaveBeenCalledWith(expect.stringContaining('threadId'), 'pipe-no-map')
    })

    it('无 threadId 但有 pipelineSessionMap：回退查表成功（1056 回退面）', async () => {
      const store = usePipelineMessageStore.getState()
      setApiRecords([{ id: 'a1', sequence: 1, role: 'assistant', content: 'x', timestamp: '2026-01-01T00:00:00Z' }])
      await store.fetchMessages(PIPE)
      expect(mockGet.mock.calls[0][0]).toContain(THREAD)
      expect(store.getMessages(PIPE)).toHaveLength(1)
    })

    it('翻页 404：debug 记录不告警、异常照抛、loading 标记在 finally 复位（1114-1115/1136-1137）', async () => {
      const store = usePipelineMessageStore.getState()
      mockGet.mockRejectedValueOnce(Object.assign(new Error('nf'), { response: { status: 404 } }))
      const pending = store.fetchMessages(PIPE, { threadId: THREAD, before_sequence: 1 })
      await expect(pending).rejects.toThrow('nf')
      expect(usePipelineMessageStore.getState().isLoadingOlderByPipeline[PIPE]).toBe(false)
      expect(log.debug).toHaveBeenCalledWith(expect.stringContaining('404'), PIPE)
      expect(log.warn).not.toHaveBeenCalled()
    })

    it('网络错误（无 status 有 code）：warn 携带可读错误码而非 [object Object]（1119）', async () => {
      const store = usePipelineMessageStore.getState()
      mockGet.mockRejectedValueOnce(Object.assign(new Error('timeout'), { code: 'ECONNABORTED' }))
      await expect(store.fetchMessages(PIPE, { threadId: THREAD })).rejects.toThrow('timeout')
      expect(log.warn).toHaveBeenCalledWith(
        expect.stringContaining('fetchMessages'),
        PIPE,
        'ECONNABORTED',
      )
      // 无 status 无 code：errInfo 回落 err.message
      mockGet.mockRejectedValueOnce(new Error('plain-boom'))
      await expect(store.fetchMessages(PIPE, { threadId: THREAD })).rejects.toThrow('plain-boom')
      expect(log.warn).toHaveBeenCalledWith(
        expect.stringContaining('fetchMessages'),
        PIPE,
        'plain-boom',
      )
    })

    it('响应迟到前被同管道新请求取消且以拒绝收场：静默返回不写状态、不上抛（1112）', async () => {
      const store = usePipelineMessageStore.getState()
      let rejectFirst!: (e: unknown) => void
      mockGet.mockImplementationOnce(
        () => new Promise((_, reject) => { rejectFirst = reject }),
      )
      const first = store.fetchMessages(PIPE, { threadId: THREAD })
      setApiRecords([{ id: 'b1', sequence: 1, role: 'assistant', content: 'b', timestamp: '2026-01-01T00:00:00Z' }])
      await store.fetchMessages(PIPE, { threadId: THREAD })
      rejectFirst(new Error('canceled'))
      await expect(first).resolves.toBeUndefined()
      expect(store.getMessages(PIPE).map((m) => m.id)).toEqual(['b1'])
    })
  })

  describe('transient 中间态重建流式占位（fetchMessages init 路径）', () => {
    it('非 chunk 键/空 id/权威命中/本地命中跳过，存活 chunk 重建且内容只取 text 块', async () => {
      const store = usePipelineMessageStore.getState()
      // 本地在飞 user（新鲜窗口内，initFromAPI 保留）→ existingIds 侧命中源
      store.addMessage(PIPE, {
        id: 'local-x', sessionId: THREAD, role: 'user', content: 'q',
        timestamp: new Date().toISOString(), status: 'sending', clientMessageId: 'cm-x',
      } as Message)
      setApiRecords(
        [{ id: 'm1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' }],
        false,
        {
          transient_states: [
            { key: 'progress:pm1', value: { blocks: [{ type: 'text', content: 'nope' }] } },
            { key: 'chunk:', value: {} },
            { key: 'chunk:m9' },
            { key: 'chunk:m1', value: { blocks: [{ type: 'text', content: 'dup' }] } },
            { key: 'chunk:m8', value: { blocks: 'corrupt' } },
            { key: 'chunk:m7', value: { blocks: [{ type: 'text', content: 'a' }, null, { type: 'thinking' }] } },
            { key: 'chunk:local-x', value: { blocks: [{ type: 'text', content: 'ghost' }] } },
          ],
        },
      )
      await store.fetchMessages(PIPE, { threadId: THREAD })
      const msgs = store.getMessages(PIPE)
      expect(msgs.map((m) => m.id)).toEqual(['m1', 'local-x', 'm9', 'm8', 'm7'])
      expect(msgs.find((m) => m.id === 'm7')?.content).toBe('a')
      expect(msgs.find((m) => m.id === 'm9')?.status).toBe('streaming')
    })
  })

  describe('loadPipelineMessages 模式决策缺省面', () => {
    it('mode=init 作用于未知管道：existingCount 缺省 0、走全量（1162）', async () => {
      const store = usePipelineMessageStore.getState()
      setApiRecords([{ id: 'a1', sequence: 1, role: 'assistant', content: 'x', timestamp: '2026-01-01T00:00:00Z' }])
      const result = await store.loadPipelineMessages('pipe-fresh', { threadId: 't-fresh', mode: 'init' })
      expect(result.ok).toBe(true)
      expect(mockGet.mock.calls[0][0]).toContain('t-fresh')
      expect(store.getMessages('pipe-fresh')).toHaveLength(1)
    })

    it('mode=backfill 且无 bottomCursor：after_sequence 缺省 0（1192）', async () => {
      setApiRecords([])
      const result = await usePipelineMessageStore.getState().loadPipelineMessages('pipe-bf', {
        threadId: 't-bf',
        mode: 'backfill',
      })
      expect(result.ok).toBe(true)
      expect(mockGet.mock.calls[0][1].params.after_sequence).toBe(0)
    })
  })

  describe('查询方法与游标缺省面', () => {
    it('未注册管道：游标 0 / hasMoreOlder false / findLastUserMessageId null / 各操作静默', () => {
      const store = usePipelineMessageStore.getState()
      expect(store.getTopCursor('nope')).toBe(0)
      expect(store.getBottomCursor('nope')).toBe(0)
      expect(store.hasMoreOlder('nope')).toBe(false)
      expect(store.findLastUserMessageId('nope')).toBeNull()
      expect(() => store.removeMessage('nope', 'm1')).not.toThrow()
      expect(store.confirmUserMessage('nope', 'cm-x')).toBe(false)
    })

    it('removeMessage/truncateMessagesAfter 目标缺失：幂等跳过（713/739）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [
        makeMsg('u1', 1, { role: 'user', content: 'q' }),
        makeMsg('a1', 2),
      ])
      store.removeMessage(PIPE, 'no-such')
      expect(store.getMessages(PIPE)).toHaveLength(2)
      store.truncateMessagesAfter(PIPE, 'no-such')
      expect(store.getMessages(PIPE)).toHaveLength(2)
    })

    it('appendMessages 空数组 no-op；未知管道直接建列表（963/965）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('u1', 1, { role: 'user', content: 'q' })])
      const before = store.getMessages(PIPE)
      store.appendMessages(PIPE, [])
      expect(store.getMessages(PIPE)).toEqual(before)
      store.appendMessages('pipe-inc', [makeMsg('n1', 5)])
      expect(store.getMessages('pipe-inc').map((m) => m.id)).toEqual(['n1'])
    })

    it('appendMessages 全空白 API 消息：消息可并入但游标不被空值推进（370）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [
        makeMsg('u1', 1, { role: 'user', content: 'q' }),
        makeMsg('a1', 2),
      ])
      store.appendMessages(PIPE, [makeMsg('blank', 3, { content: '' })])
      expect(store.getBottomCursor(PIPE)).toBe(2)
    })

    it('增量补漏按 recordId 双键收敛：API 版让本地让位（332/333）', () => {
      const store = usePipelineMessageStore.getState()
      const REC = 'mc_abc'
      // 本地已认领 user（UI id=uuid、recordId=权威指纹）
      store.addMessage(PIPE, makeMsg('uuid-1', 1, {
        role: 'user', content: 'q', recordId: REC, clientMessageId: 'cm-1', status: 'completed',
      }))
      // API 增量：同 recordId 不同 id 的权威版
      store.appendMessages(PIPE, [
        { id: 'srv-9', sequence: 1, role: 'user', content: 'q', recordId: REC, timestamp: '2026-01-01T00:00:00Z' } as Message,
      ])
      const users = store.getMessages(PIPE).filter((m) => m.role === 'user')
      expect(users).toHaveLength(1)
      expect(users[0].id).toBe('srv-9')
    })

    it('prependMessages 无 hasMoreOlder 入参：hasMoreOlder 置 false；无 sequence 消息 topCursor 落 0（950/937）', () => {
      const store = usePipelineMessageStore.getState()
      store.prependMessages('pipe-ns', [
        { id: 'n1', sessionId: 's', role: 'user', content: 'a', timestamp: '2026-01-01T00:00:00Z' } as Message,
        { id: 'n2', sessionId: 's', role: 'assistant', content: 'b', timestamp: '2026-01-01T00:00:01Z' } as Message,
      ])
      expect(store.hasMoreOlder('pipe-ns')).toBe(false)
      expect(store.getTopCursor('pipe-ns')).toBe(0)
      expect(store.getMessages('pipe-ns')).toHaveLength(2)
    })

    it('initFromAPI：空消息列表与无 sequence 消息 topCursor 均为 0（884 双面）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-empty', [])
      expect(store.getTopCursor('pipe-empty')).toBe(0)
      store.initFromAPI('pipe-noseq', [{ ...makeMsg('n1'), sequence: undefined } as Message])
      expect(store.getTopCursor('pipe-noseq')).toBe(0)
    })
  })

  describe('claimUserMessage / ensureInjectedUserMessage / confirmUserMessage', () => {
    const CMID = 'cm-claim-gap'
    const REC = 'mc_claim_gap'

    it('认领入参无权威 seq：upgrade 保留原 sequence（562）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, {
        id: CMID, sessionId: THREAD, role: 'user', content: 'q',
        timestamp: new Date().toISOString(), status: 'sending', clientMessageId: CMID, sequence: 2,
      } as Message)
      const result = store.claimUserMessage(PIPE, CMID, { id: REC, content: 'q' })
      expect(result).toBe('upgraded')
      const user = store.getMessages(PIPE)[0]
      expect(user.recordId).toBe(REC)
      expect(user.status).toBe('completed')
      expect(user.sequence).toBe(2)
    })

    it('insert 幂等：同 id 权威版已在主数组 → 不重复补插（583/584）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, makeMsg(CMID, 1, { role: 'user', content: 'q', status: 'completed' }))
      const result = store.claimUserMessage(PIPE, CMID, { id: REC, content: 'q', sequence: 4 })
      expect(result).toBe('inserted')
      const msgs = store.getMessages(PIPE)
      expect(msgs).toHaveLength(1)
      expect(msgs[0].recordId).toBeUndefined()
    })

    it('insert：content 非字符串回落空串（595）', () => {
      const store = usePipelineMessageStore.getState()
      const result = store.claimUserMessage('pipe-raw', CMID, { id: REC, content: 42, sequence: 1 })
      expect(result).toBe('inserted')
      expect(store.getMessages('pipe-raw')[0].content).toBe('')
    })

    it('ensureInjectedUserMessage：无 id 直接返回；未知管道建立列表；content 非字符串/无 seq/metadata 全缺省', () => {
      const store = usePipelineMessageStore.getState()
      store.ensureInjectedUserMessage(PIPE, { content: 'no-id' })
      expect(store.getMessages(PIPE)).toEqual([])

      store.ensureInjectedUserMessage('pipe-virgin', { id: 'r1', content: 'hi' })
      expect(store.getMessages('pipe-virgin').map((m) => m.id)).toEqual(['r1'])

      store.ensureInjectedUserMessage('pipe-virgin', { id: 'r2', content: { deep: true }, metadata: { k: 1 } })
      const r2 = store.getMessages('pipe-virgin').find((m) => m.id === 'r2')
      expect(r2?.content).toBe('')
      expect(r2?.sequence).toBeUndefined()
      expect(r2?.metadata).toEqual({ k: 1 })

      store.ensureInjectedUserMessage('pipe-virgin', { id: 'r1', content: 'dup' })
      expect(store.getMessages('pipe-virgin')).toHaveLength(2)
    })

    it('ensureInjectedUserMessage：按 sequence 落位到首个更大 seq 之前；更大 seq 追加尾部（638 双面）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI('pipe-seq', [
        makeMsg('u10', 10, { role: 'user', content: 'q' }),
        makeMsg('a20', 20),
      ])
      store.ensureInjectedUserMessage('pipe-seq', { id: 'r15', content: 'mid', sequence: 15 })
      expect(store.getMessages('pipe-seq').map((m) => m.id)).toEqual(['u10', 'r15', 'a20'])
      store.ensureInjectedUserMessage('pipe-seq', { id: 'r30', content: 'late', sequence: 30 })
      expect(store.getMessages('pipe-seq').map((m) => m.id)).toEqual(['u10', 'r15', 'a20', 'r30'])
    })

    it('confirmUserMessage：已 completed 与未找到均返回 false（657 双面）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, makeMsg('done-u', 1, {
        role: 'user', content: 'q', clientMessageId: 'cm-done', status: 'completed',
      }))
      expect(store.confirmUserMessage(PIPE, 'cm-done')).toBe(false)
      expect(store.confirmUserMessage(PIPE, 'cm-none')).toBe(false)
    })
  })

  describe('Parts 方法负面路径', () => {
    const textPart = (content: string, state = 'done') => ({ type: 'text', content, state }) as MessagePart
    const toolPart = (callId: string, state: string) =>
      ({ type: 'tool_call', callId, name: 't', state }) as MessagePart
    const systemPart = (level: string) => ({ type: 'system', level, content: 'n' }) as MessagePart

    function seed() {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [
        makeMsg('m-text', 1, { parts: [textPart('a', 'streaming')] }),
        makeMsg('m-noparts', 2),
        makeMsg('m-tool', 3, { content: '', parts: [toolPart('c1', 'calling')] }),
        makeMsg('m-mix', 4, {
          content: '',
          parts: [toolPart('c2', 'error'), toolPart('c3', 'streaming'), systemPart('info')],
        }),
      ])
      return store
    }

    it('updatePart：未知管道/未知消息/无 parts/越界均 no-op，合法索引生效', () => {
      const store = seed()
      store.updatePart('nope', 'm-text', 0, { content: 'x' } as never)
      store.updatePart(PIPE, 'no-msg', 0, { content: 'x' } as never)
      store.updatePart(PIPE, 'm-noparts', 0, { content: 'x' } as never)
      store.updatePart(PIPE, 'm-text', -1, { content: 'x' } as never)
      store.updatePart(PIPE, 'm-text', 9, { content: 'x' } as never)
      expect((store.getMessages(PIPE).find((m) => m.id === 'm-text')?.parts as MessagePart[])[0].content).toBe('a')
      store.updatePart(PIPE, 'm-text', 0, { content: 'z' } as never)
      expect((store.getMessages(PIPE).find((m) => m.id === 'm-text')?.parts as MessagePart[])[0].content).toBe('z')
    })

    it('appendToPart：负面路径 no-op；tool_call part 拒绝追加（1266）', () => {
      const store = seed()
      store.appendToPart('nope', 'm-text', 0, 'x')
      store.appendToPart(PIPE, 'no-msg', 0, 'x')
      store.appendToPart(PIPE, 'm-noparts', 0, 'x')
      store.appendToPart(PIPE, 'm-text', 9, 'x')
      expect((store.getMessages(PIPE).find((m) => m.id === 'm-text')?.parts as MessagePart[])[0].content).toBe('a')
      store.appendToPart(PIPE, 'm-tool', 0, 'x')
      expect(store.getMessages(PIPE).find((m) => m.id === 'm-tool')?.parts).toHaveLength(1)
      store.appendToPart(PIPE, 'm-text', 0, 'B')
      expect((store.getMessages(PIPE).find((m) => m.id === 'm-text')?.parts as MessagePart[])[0].content).toBe('aB')
    })

    it('finalizeMessage：text → done、error 工具保持 error、streaming 工具 → done、system 原样（1300/1303/1306）', () => {
      const store = seed()
      store.finalizeMessage('nope', 'm-text')
      store.finalizeMessage(PIPE, 'm-mix')
      const parts = store.getMessages(PIPE).find((m) => m.id === 'm-mix')?.parts as MessagePart[]
      expect(parts[0].state).toBe('error')
      expect(parts[1].state).toBe('done')
      expect(parts[2].type).toBe('system')
      expect((parts[2] as { content?: string }).content).toBe('n')

      store.finalizeMessage(PIPE, 'm-text')
      expect((store.getMessages(PIPE).find((m) => m.id === 'm-text')?.parts as MessagePart[])[0].state).toBe('done')
    })

    it('findLastPartIndex / findStreamingPartIndex：各级缺省与未命中均 -1（1328/1330/1334/1341）', () => {
      const store = seed()
      expect(store.findLastPartIndex('nope', 'm-text', 'text')).toBe(-1)
      expect(store.findLastPartIndex(PIPE, 'no-msg', 'text')).toBe(-1)
      expect(store.findLastPartIndex(PIPE, 'm-noparts', 'text')).toBe(-1)
      expect(store.findLastPartIndex(PIPE, 'm-mix', 'thinking')).toBe(-1)
      expect(store.findLastPartIndex(PIPE, 'm-mix', 'tool_call')).toBe(1)
      expect(store.findStreamingPartIndex('nope', 'm-text')).toBe(-1)
      expect(store.findStreamingPartIndex(PIPE, 'no-msg')).toBe(-1)
      expect(store.findStreamingPartIndex(PIPE, 'm-noparts')).toBe(-1)
      expect(store.findStreamingPartIndex(PIPE, 'm-tool')).toBe(-1)
      expect(store.findStreamingPartIndex(PIPE, 'm-text')).toBe(0)
    })
  })

  describe('persist merge / partialize / rehydrate 清理', () => {
    it('merge：null 快照走缺省；streaming 消息与空条目剔除；运行时状态强制重置', () => {
      const options = usePipelineMessageStore.persist.getOptions()
      const base = usePipelineMessageStore.getInitialState()

      const fromNull = options.merge?.(undefined, base)
      expect(fromNull?.reconciledByPipeline).toEqual({})

      const merged = options.merge?.(
        {
          messagesByPipeline: {
            keep: [makeMsg('s1', 1, { status: 'streaming' }), makeMsg('d1', 2)],
            nil: null,
          },
          bottomCursorsByPipeline: { keep: 5 },
        },
        base,
      ) as Record<string, unknown>
      const kept = (merged.messagesByPipeline as Record<string, Message[]>).keep
      expect(kept.map((m) => m.id)).toEqual(['d1'])
      expect((merged.messagesByPipeline as Record<string, Message[]>).nil).toBeUndefined()
      expect(merged.streamingState).toEqual({})
      expect(merged.isLoadingOlderByPipeline).toEqual({})
      expect(merged.reconciledByPipeline).toEqual({})
      expect(merged.bottomCursorsByPipeline).toEqual({ keep: 5 })
    })

    it('partialize：仅保留持久化集合内的管道元数据；缺省记录回退空对象（1384）', () => {
      const options = usePipelineMessageStore.persist.getOptions()
      const base = usePipelineMessageStore.getInitialState()
      const state = {
        ...base,
        messagesByPipeline: { p1: [makeMsg('d1', 1)], pDrop: [makeMsg('d2', 2)] },
        pipelines: { p1: { pipelineId: 'p1', sessionId: 's', level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 } },
        pipelineSessionMap: { p1: 's' },
        topCursorsByPipeline: { p1: 1 },
        bottomCursorsByPipeline: { p1: 1 },
        hasMoreOlderByPipeline: { p1: true },
        activePipelineId: 'p1',
        streamingState: { p1: { isStreaming: true, messageId: 'd1' } },
        isLoadingOlderByPipeline: { p1: true },
        reconciledByPipeline: { p1: true },
      } as never
      const partial = options.partialize?.(state) as Record<string, unknown>
      expect(Object.keys(partial.messagesByPipeline as object).sort()).toEqual(['p1', 'pDrop'])
      expect(Object.keys(partial.pipelines as object)).toEqual(['p1'])
      expect(partial.activePipelineId).toBe('p1')
      expect(partial.streamingState).toBeUndefined()
      expect(partial.reconciledByPipeline).toBeUndefined()

      const bare = options.partialize?.({ ...base, pipelines: undefined, pipelineSessionMap: undefined } as never) as Record<string, unknown>
      expect(bare.pipelines).toEqual({})
      expect(bare.pipelineSessionMap).toEqual({})
    })

    it('rehydrate 完成回调：清理旧 localStorage key（有则删、无则跳过）', async () => {
      window.localStorage.setItem('pipeline-messages', '{"state":{}}')
      storageGet.mockResolvedValueOnce(null)
      await usePipelineMessageStore.persist.rehydrate()
      expect(window.localStorage.getItem('pipeline-messages')).toBeNull()

      storageGet.mockResolvedValueOnce(null)
      await usePipelineMessageStore.persist.rehydrate()
      expect(window.localStorage.getItem('pipeline-messages')).toBeNull()
    })
  })

  it('isStreaming：流式态查询缺省 false（对照面）', () => {
    const store = usePipelineMessageStore.getState()
    expect(store.isStreaming(PIPE)).toBe(false)
  })

  describe('动作成功路径与注册/激活缺省面', () => {
    it('confirmUserMessage：sending 乐观 user 认领成功返回 true（658-661）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, makeMsg('pending-u', 1, {
        role: 'user', content: 'q', clientMessageId: 'cm-ok', status: 'sending',
      }))
      expect(store.confirmUserMessage(PIPE, 'cm-ok')).toBe(true)
      expect(store.getMessages(PIPE)[0].status).toBe('completed')
    })

    it('removeMessage：命中即移除（714-715）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('u1', 1, { role: 'user', content: 'q' }), makeMsg('a1', 2)])
      store.removeMessage(PIPE, 'a1')
      expect(store.getMessages(PIPE).map((m) => m.id)).toEqual(['u1'])
    })

    it('truncateMessagesAfter：保留到目标 user 消息（含）为止（742-743）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [
        makeMsg('u1', 1, { role: 'user', content: 'q1' }),
        makeMsg('u2', 2, { role: 'user', content: 'q2' }),
        makeMsg('a3', 3),
      ])
      store.truncateMessagesAfter(PIPE, 'u2')
      expect(store.getMessages(PIPE).map((m) => m.id)).toEqual(['u1', 'u2'])
    })

    it('findLastUserMessageId：仅 assistant 时遍历耗尽返回 null（729）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('a1', 1), makeMsg('a2', 2)])
      expect(store.findLastUserMessageId(PIPE)).toBeNull()
    })

    it('stopStreaming：流式 assistant 收尾为 interrupted，text → done、tool_call calling → error（796/799）', () => {
      const store = usePipelineMessageStore.getState()
      store.startStreaming(PIPE, 'm-live')
      store.addMessage(PIPE, makeMsg('m-live', 1, {
        content: '',
        parts: [
          { type: 'text', content: 'half', state: 'streaming' },
          { type: 'tool_call', callId: 'c1', name: 't', state: 'calling' },
          { type: 'tool_call', callId: 'c2', name: 't', state: 'done' },
        ] as MessagePart[],
      }))
      store.stopStreaming(PIPE)
      const msg = store.getMessages(PIPE).find((m) => m.id === 'm-live')
      expect(msg?.status).toBe('interrupted')
      const parts = msg?.parts as MessagePart[]
      expect(parts[0].state).toBe('done')
      expect(parts[1].state).toBe('error')
      expect(parts[2].state).toBe('done')
      expect(store.isStreaming(PIPE)).toBe(false)
    })

    it('prependMessages 空数组：仅落 hasMoreOlder=false 与 loading 复位（915-925）', () => {
      const store = usePipelineMessageStore.getState()
      usePipelineMessageStore.setState({
        isLoadingOlderByPipeline: { [PIPE]: true },
        hasMoreOlderByPipeline: { [PIPE]: true },
      })
      store.prependMessages(PIPE, [])
      expect(store.hasMoreOlder(PIPE)).toBe(false)
      expect(usePipelineMessageStore.getState().isLoadingOlderByPipeline[PIPE]).toBe(false)
      expect(store.getMessages(PIPE)).toEqual([])
    })

    it('appendPart：未知管道/未知消息 no-op，命中即追加（1208/1210）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('m1', 1)])
      store.appendPart('nope', 'm1', { type: 'text', content: 'x' } as MessagePart)
      store.appendPart(PIPE, 'no-msg', { type: 'text', content: 'x' } as MessagePart)
      expect(store.getMessages(PIPE)[0].parts).toBeUndefined()
      store.appendPart(PIPE, 'm1', { type: 'text', content: 'x', state: 'streaming' } as MessagePart)
      expect((store.getMessages(PIPE)[0].parts as MessagePart[])[0].content).toBe('x')
    })

    it('findToolCallPartIndex：缺省 -1、按 callId 命中、未命中 -1（1358-1363）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [
        makeMsg('m1', 1, {
          content: '',
          parts: [
            { type: 'text', content: 'a' },
            { type: 'tool_call', callId: 'c9', name: 't' },
          ] as MessagePart[],
        }),
        makeMsg('m2', 2),
      ])
      expect(store.findToolCallPartIndex('nope', 'm1', 'c9')).toBe(-1)
      expect(store.findToolCallPartIndex(PIPE, 'no-msg', 'c9')).toBe(-1)
      expect(store.findToolCallPartIndex(PIPE, 'm2', 'c9')).toBe(-1)
      expect(store.findToolCallPartIndex(PIPE, 'm1', 'c1')).toBe(-1)
      expect(store.findToolCallPartIndex(PIPE, 'm1', 'c9')).toBe(1)
    })

    it('updateMessage：未知管道 no-op（674）；finalizeMessage：未知消息 no-op（1293）', () => {
      const store = usePipelineMessageStore.getState()
      store.initFromAPI(PIPE, [makeMsg('m1', 1)])
      store.updateMessage('nope', 'm1', { content: 'x' })
      expect(store.getMessages(PIPE)[0].content).toBe('c')
      store.finalizeMessage(PIPE, 'no-msg')
      expect(store.getMessages(PIPE)[0].status).toBe('completed')
    })

    it('registerPipeline 重复注册保留未读计数；activatePipeline 未注册管道不建元数据（439/461）', () => {
      const store = usePipelineMessageStore.getState()
      const meta = (unread: number) => ({
        pipelineId: PIPE, sessionId: THREAD, level: 1 as const, tabId: null,
        agentName: '', status: 'idle' as const, parentId: null, unreadCount: unread,
      })
      store.registerPipeline(meta(3))
      store.registerPipeline(meta(0))
      expect(usePipelineMessageStore.getState().pipelines[PIPE].unreadCount).toBe(3)

      usePipelineMessageStore.setState({ pipelines: {} })
      store.activatePipeline('pipe-unknown')
      expect(usePipelineMessageStore.getState().pipelines).toEqual({})
      expect(usePipelineMessageStore.getState().activePipelineId).toBe('pipe-unknown')
    })

    it('claim：候选按 recordId 命中且已带同 recordId → skipped 幂等（583 关联）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, makeMsg('srv-1', 1, {
        role: 'user', content: 'q', recordId: 'mc_dup', status: 'completed',
      }))
      const result = store.claimUserMessage(PIPE, 'cm-unknown', { id: 'mc_dup', content: 'q', sequence: 9 })
      expect(result).toBe('skipped')
      expect(store.getMessages(PIPE)).toHaveLength(1)
      expect(store.getMessages(PIPE)[0].id).toBe('srv-1')
    })

    it('initFromAPI：API 消息带 clientMessageId 时进入回传索引（861）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, {
        id: 'uuid-x', sessionId: THREAD, role: 'user', content: 'q',
        timestamp: new Date().toISOString(), status: 'sending', clientMessageId: 'cm-live',
      } as Message)
      store.initFromAPI(PIPE, [
        { id: 'srv-x', sequence: 1, role: 'user', content: 'q', clientMessageId: 'cm-live', timestamp: '2026-01-01T00:00:00Z' } as Message,
      ])
      const users = store.getMessages(PIPE).filter((m) => m.role === 'user')
      expect(users).toHaveLength(1)
      expect(users[0].id).toBe('srv-x')
    })

    it('initFromAPI：本地 recordId 命中 API id 集时让位权威版（332）', () => {
      const store = usePipelineMessageStore.getState()
      store.addMessage(PIPE, makeMsg('uuid-y', 1, {
        role: 'user', content: 'q', recordId: 'mc_hit', clientMessageId: 'cm-y', status: 'completed',
      }))
      store.initFromAPI(PIPE, [
        { id: 'mc_hit', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' } as Message,
      ])
      const users = store.getMessages(PIPE).filter((m) => m.role === 'user')
      expect(users).toHaveLength(1)
      expect(users[0].id).toBe('mc_hit')
    })
  })
})

describe('水合等待（waitForMessageHydration）超时兜底', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    mockGet.mockReset()
    vi.resetModules()
    storageGet.mockReset()
    // 水合信号永不完成：覆盖 500ms 兜底路径
    storageGet.mockImplementation(() => new Promise<void>(() => {}))
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
      reconciledByPipeline: {},
    })
  })

  it('rehydrate 迟迟未完成：auto 加载等满 500ms 兜底后照常发起全量请求', async () => {
    vi.useFakeTimers()
    try {
      setApiRecords([{ id: 'a1', sequence: 1, role: 'assistant', content: 'x', timestamp: '2026-01-01T00:00:00Z' }])
      const pending = usePipelineMessageStore.getState().loadPipelineMessages('p-hy', { threadId: 't-hy' })
      await vi.advanceTimersByTimeAsync(501)
      const result = await pending
      expect(result.ok).toBe(true)
      expect(mockGet).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('等待期间 rehydrate 完成：等待者立即放行，迟到的兜底定时器空转不误伤', async () => {
    vi.useFakeTimers()
    try {
      setApiRecords([{ id: 'a1', sequence: 1, role: 'assistant', content: 'x', timestamp: '2026-01-01T00:00:00Z' }])
      const pending = usePipelineMessageStore.getState().loadPipelineMessages('p-hy', { threadId: 't-hy' })
      storageGet.mockResolvedValueOnce(null)
      await usePipelineMessageStore.persist.rehydrate()
      const result = await pending
      expect(result.ok).toBe(true)
      expect(mockGet).toHaveBeenCalledTimes(1)
      // 已放行后兜底定时器仍会触发：waiter 已被取走，不产生二次副作用
      await vi.advanceTimersByTimeAsync(501)
      expect(mockGet).toHaveBeenCalledTimes(1)

      // 已水合：后续 auto 加载即时放行（不等待、直接请求）
      setApiRecords([{ id: 'b1', sequence: 1, role: 'assistant', content: 'y', timestamp: '2026-01-01T00:00:00Z' }])
      const second = await usePipelineMessageStore.getState().loadPipelineMessages('p-hy-2', { threadId: 't-hy-2' })
      expect(second.ok).toBe(true)
      expect(mockGet).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })
})
