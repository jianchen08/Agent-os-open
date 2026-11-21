// @feature: FP-0.2.流式活性统一 | @ci: frontend-test
/**
 * 等待 AI 响应壳（awaitingReplyByPipeline）store 面测试：
 * 置位（claimUserMessage 认领成功且未流式，insert/upgrade 双路）、skip 不置位、
 * 流式中不置位、startStreaming/stopStreaming 双清口、TTL 到期自动清除、
 * markAwaitingReply 流式中幂等零动作、clearAwaitingReply 幂等。
 *
 * mock 边界仅限网络（apiClient）与持久化存储（indexedDbStorage）；断言输入→输出。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn(async () => []),
}))

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
  createDeferredIndexedDbStorage: () => ({
    getItem: storageGet,
    setItem: storageSet,
    removeItem: storageRemove,
  }),
}))

const PIPE = 'pipe-await-001'
const THREAD = 'thread-await-001'
const CMID = 'cmid-await-001'

function makeUserMsg(id: string, overrides: Partial<Message> = {}): Message {
  return {
    id,
    sessionId: THREAD,
    role: 'user',
    content: 'hello',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
    clientMessageId: CMID,
    ...overrides,
  } as Message
}

describe('pipelineMessageStore 等待 AI 响应壳', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    mockGet.mockReset()
    vi.resetModules()
    vi.useRealTimers()
    storageGet.mockReset()
    storageGet.mockImplementation(() => new Promise<void>(() => {}))
    storageSet.mockResolvedValue(undefined)
    storageRemove.mockResolvedValue(undefined)
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPE]: THREAD },
      streamingState: {},
      activePipelineId: null,
      awaitingReplyByPipeline: {},
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const flag = () => usePipelineMessageStore.getState().awaitingReplyByPipeline?.[PIPE]

  it('claimUserMessage insert 路径置位（未流式）', () => {
    const ret = usePipelineMessageStore.getState().claimUserMessage(PIPE, CMID, {
      id: 'mc_abc',
      content: 'hello',
      sequence: 7,
    })
    expect(ret).toBe('inserted')
    expect(flag()).toBeGreaterThan(0)
    // 气泡上屏与壳置位同事务：user 消息确实进了主数组
    expect(
      usePipelineMessageStore.getState().getMessages(PIPE).some((m) => m.role === 'user'),
    ).toBe(true)
  })

  it('claimUserMessage upgrade 路径置位（候选在场未认领）', () => {
    usePipelineMessageStore.setState({
      messagesByPipeline: { [PIPE]: [makeUserMsg('ui-1')] },
    })
    const ret = usePipelineMessageStore.getState().claimUserMessage(PIPE, CMID, {
      id: 'mc_abc',
      content: 'hello',
      sequence: 7,
    })
    expect(ret).toBe('upgraded')
    expect(flag()).toBeGreaterThan(0)
  })

  it('claimUserMessage skip（无权威 id）不置位', () => {
    const ret = usePipelineMessageStore.getState().claimUserMessage(PIPE, CMID, {
      content: 'hello',
    })
    expect(ret).toBe('skipped')
    expect(flag()).toBeUndefined()
  })

  it('流式中 claimUserMessage（new_message 认领回传链）不置位', () => {
    usePipelineMessageStore.setState({
      streamingState: { [PIPE]: { isStreaming: true, messageId: 'a_1', startedAt: Date.now() } },
    })
    usePipelineMessageStore.getState().claimUserMessage(PIPE, CMID, {
      id: 'mc_abc',
      content: 'hello',
      sequence: 7,
    })
    expect(flag()).toBeUndefined()
  })

  it('markAwaitingReply 流式中零动作', () => {
    usePipelineMessageStore.setState({
      streamingState: { [PIPE]: { isStreaming: true, messageId: 'a_1', startedAt: Date.now() } },
    })
    usePipelineMessageStore.getState().markAwaitingReply(PIPE)
    expect(flag()).toBeUndefined()
  })

  it('startStreaming 清除（stream_start 真气泡接管）', () => {
    usePipelineMessageStore.getState().markAwaitingReply(PIPE)
    expect(flag()).toBeGreaterThan(0)
    usePipelineMessageStore.getState().startStreaming(PIPE, 'a_1')
    expect(flag()).toBeUndefined()
  })

  it('stopStreaming 清除（stream_end/stream_error/terminatePipeline 漏斗）', () => {
    usePipelineMessageStore.getState().markAwaitingReply(PIPE)
    usePipelineMessageStore.getState().stopStreaming(PIPE)
    expect(flag()).toBeUndefined()
  })

  it('TTL 到期自动清除（防引擎死亡悬挂）', () => {
    vi.useFakeTimers()
    usePipelineMessageStore.getState().markAwaitingReply(PIPE)
    expect(flag()).toBeGreaterThan(0)
    vi.advanceTimersByTime(60_000)
    expect(flag()).toBeUndefined()
  })

  it('clearAwaitingReply 幂等（无标记重复清除不报错）', () => {
    expect(() => usePipelineMessageStore.getState().clearAwaitingReply(PIPE)).not.toThrow()
    usePipelineMessageStore.getState().markAwaitingReply(PIPE)
    usePipelineMessageStore.getState().clearAwaitingReply(PIPE)
    expect(flag()).toBeUndefined()
    usePipelineMessageStore.getState().clearAwaitingReply(PIPE)
    expect(flag()).toBeUndefined()
  })
})
