// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * chatInputStore 测试
 *
 * chatInputStore：pendingInsert 桥接（request/consume）、草稿 CRUD + 持久化、
 * 激活任务模式的会话级记忆（taskModes）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
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
    const raw = localStorage.getItem('chat-input-drafts') || ''
    const parsed = JSON.parse(raw)
    expect(parsed.state.drafts['persist-key']).toBe('持久化内容')
    expect(parsed.state.pendingInsert).toBeUndefined()
  })

  it('setTaskMode 按 key 记忆激活模式；null 清除且不占位（缺键 = 自动）', () => {
    expect(store.getState().taskModes['tab-1']).toBeUndefined()
    store.getState().setTaskMode('tab-1', 'writing')
    store.getState().setTaskMode('tab-2', 'coding')
    expect(store.getState().taskModes).toEqual({ 'tab-1': 'writing', 'tab-2': 'coding' })
    store.getState().setTaskMode('tab-1', null)
    expect(store.getState().taskModes).toEqual({ 'tab-2': 'coding' })
    // 同 key 覆盖切换
    store.getState().setTaskMode('tab-2', 'research')
    expect(store.getState().taskModes['tab-2']).toBe('research')
  })

  it('taskModes 为内存态，不随 persist 落盘（partialize 仅草稿）', () => {
    store.getState().setTaskMode('k', 'writing')
    store.getState().saveDraft('d', '正文')
    const parsed = JSON.parse(localStorage.getItem('chat-input-drafts') || '{}')
    expect(parsed.state.drafts['d']).toBe('正文')
    expect(parsed.state.taskModes).toBeUndefined()
  })
})
