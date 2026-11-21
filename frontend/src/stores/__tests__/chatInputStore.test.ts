// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * chatInputStore 测试
 *
 * chatInputStore：pendingInsert 桥接（request/consume）、草稿 CRUD + 持久化。
 * （激活任务模式的会话级记忆已随批 G④ 出生语义退役——选择器选择 = 经
 * modeSessionBinder 开新会话，无内存态档位。）
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { userScopedKey } from '@/utils/userScopedStorage'
import type * as chatInputStoreMod from '@/stores/chatInputStore'

vi.mock('@/utils/logger', () => ({
  loggers: { storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

describe('chatInputStore - 输入框桥接与草稿', () => {
  let store: chatInputStoreMod.useChatInputStore

  beforeEach(async () => {
    localStorage.clear()
    vi.resetModules()
    store = (await import('@/stores/chatInputStore')).useChatInputStore
    store.setState({ pendingInsert: null, drafts: {} })
  })

  it('requestInsert 写入待插入文本，consumeInsert 消费后清空', () => {
    store.getState().requestInsert('待插入内容')
    expect(store.getState().pendingInsert).toBe('待插入内容')
    store.getState().consumeInsert()
    expect(store.getState().pendingInsert).toBeNull()
  })

  it('saveDraft/loadDraft 按 key 存取；loadDraft 缺省返回空串', () => {
    expect(store.getState().loadDraft('tab-1')).toBe('')
    store.getState().saveDraft('tab-1', '草稿A')
    store.getState().saveDraft('tab-2', '草稿B')
    expect(store.getState().loadDraft('tab-1')).toBe('草稿A')
    expect(store.getState().loadDraft('tab-2')).toBe('草稿B')
    expect(store.getState().loadDraft('missing')).toBe('')
  })

  it('saveDraft 同名 key 覆盖；clearDraft 只清目标 key', () => {
    store.getState().saveDraft('k1', 'v1')
    store.getState().saveDraft('k1', 'v2')
    store.getState().saveDraft('k2', 'keep')
    store.getState().clearDraft('k1')
    expect(store.getState().drafts).toEqual({ k2: 'keep' })
    expect(store.getState().loadDraft('k1')).toBe('')
  })

  it('drafts 持久化到 localStorage（partialize 仅草稿）；pendingInsert 不落盘', () => {
    store.getState().saveDraft('persist-key', '持久化内容')
    store.getState().requestInsert('瞬态桥接')
    const raw = localStorage.getItem(userScopedKey('chat-input-drafts')) || ''
    const parsed = JSON.parse(raw)
    expect(parsed.state.drafts['persist-key']).toBe('持久化内容')
    expect(parsed.state.pendingInsert).toBeUndefined()
  })
})

describe('chatInputStore - 发送在途登记（2026-11-04 落定语义：条目含快照，撤下=落定/失败/超时）', () => {
  let store: chatInputStoreMod.useChatInputStore

  beforeEach(async () => {
    localStorage.clear()
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-04T00:00:00Z'))
    vi.resetModules()
    store = (await import('@/stores/chatInputStore')).useChatInputStore
    store.setState({ sendInFlightByPipeline: {}, sendOutcomes: [] })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('beginSend 按管道挂入条目（cmid+快照）；同 cmid 幂等不重复', () => {
    store.getState().beginSend('pipe-1', 'cmid-a', '快照A')
    store.getState().beginSend('pipe-1', 'cmid-a', '快照A')
    store.getState().beginSend('pipe-1', 'cmid-b', '快照B')
    store.getState().beginSend('pipe-2', 'cmid-c')
    const pipe1 = store.getState().sendInFlightByPipeline['pipe-1']
    expect(pipe1.map((e) => e.clientMessageId)).toEqual(['cmid-a', 'cmid-b'])
    expect(pipe1[0].content).toBe('快照A')
    expect(store.getState().sendInFlightByPipeline['pipe-2'].map((e) => e.clientMessageId)).toEqual(['cmid-c'])
  })

  it('beginSend 空 pipelineId 或空 cmid 拒绝挂入', () => {
    store.getState().beginSend('', 'cmid-a')
    store.getState().beginSend('pipe-1', '')
    expect(store.getState().sendInFlightByPipeline).toEqual({})
  })

  it('landSendByCmid 跨管道精确撤下该 cmid（落定语义）', () => {
    store.getState().beginSend('pipe-1', 'cmid-a')
    store.getState().beginSend('pipe-1', 'cmid-b')
    store.getState().beginSend('pipe-2', 'cmid-c')
    store.getState().landSendByCmid('cmid-a')
    expect(store.getState().sendInFlightByPipeline['pipe-1'].map((e) => e.clientMessageId)).toEqual(['cmid-b'])
    store.getState().landSendByCmid('cmid-c')
    expect(store.getState().sendInFlightByPipeline['pipe-2']).toEqual([])
  })

  it('endSendByCmid（失败判死）跨管道精确撤下该 cmid', () => {
    store.getState().beginSend('pipe-1', 'cmid-a')
    store.getState().beginSend('pipe-2', 'cmid-c')
    store.getState().endSendByCmid('cmid-a', 'TTL 判死')
    expect(store.getState().sendInFlightByPipeline['pipe-1']).toEqual([])
    expect(store.getState().sendInFlightByPipeline['pipe-2'].map((e) => e.clientMessageId)).toEqual(['cmid-c'])
  })

  it('endSendByCmid 空 cmid 幂等零动作', () => {
    store.getState().beginSend('pipe-1', 'cmid-a')
    store.getState().endSendByCmid('')
    expect(store.getState().sendInFlightByPipeline['pipe-1'].map((e) => e.clientMessageId)).toEqual(['cmid-a'])
  })
})
