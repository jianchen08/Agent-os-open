/**
 * chatInputStore / terminationStore 测试
 *
 * chatInputStore：pendingInsert 桥接（request/consume）、草稿 CRUD + 持久化。
 * terminationStore：终止评估分桶写入/读取/清除。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('@/utils/logger', () => ({
  loggers: { storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

describe('chatInputStore - 输入框桥接与草稿', () => {
  let store: typeof import('@/stores/chatInputStore').useChatInputStore

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
})

describe('terminationStore - 终止评估状态', () => {
  let store: typeof import('@/stores/terminationStore').useTerminationStore

  beforeEach(async () => {
    vi.resetModules()
    store = (await import('@/stores/terminationStore')).useTerminationStore
    store.setState({ statusByPipeline: {} })
  })

  it('updateStatus 写入分桶状态（含 ts 时间戳），getStatus 读回', () => {
    store.getState().updateStatus('pipe-1', {
      convergence: 'converging',
      shouldStop: false,
      stopReason: '',
      remainingBudgetPercent: 60,
      iteration: 2,
      elapsedS: 10,
    })
    const status = store.getState().getStatus('pipe-1')!
    expect(status.convergence).toBe('converging')
    expect(status.shouldStop).toBe(false)
    expect(status.remainingBudgetPercent).toBe(60)
    expect(status.iteration).toBe(2)
    expect(status.elapsedS).toBe(10)
    expect(typeof status.ts).toBe('number')
  })

  it('多管道独立分桶互不覆盖', () => {
    store.getState().updateStatus('pipe-a', { convergence: 'converging', shouldStop: false, stopReason: '', remainingBudgetPercent: 80, iteration: 1, elapsedS: 5 })
    store.getState().updateStatus('pipe-b', { convergence: 'stalled', shouldStop: true, stopReason: '预算耗尽', remainingBudgetPercent: 5, iteration: 4, elapsedS: 60 })
    expect(store.getState().getStatus('pipe-a')!.convergence).toBe('converging')
    expect(store.getState().getStatus('pipe-b')).toEqual(expect.objectContaining({ convergence: 'stalled', shouldStop: true, stopReason: '预算耗尽' }))
  })

  it('clearStatus 删除指定管道；getStatus 未写入管道返回 undefined', () => {
    store.getState().updateStatus('pipe-x', { convergence: 'converging', shouldStop: false, stopReason: '', remainingBudgetPercent: null, iteration: 0, elapsedS: 0 })
    store.getState().clearStatus('pipe-x')
    expect(store.getState().getStatus('pipe-x')).toBeUndefined()
    expect(store.getState().getStatus('never-written')).toBeUndefined()
  })

  it('更新同管道覆盖旧值（不累积）', () => {
    store.getState().updateStatus('pipe-1', { convergence: 'converging', shouldStop: false, stopReason: '', remainingBudgetPercent: 90, iteration: 1, elapsedS: 3 })
    store.getState().updateStatus('pipe-1', { convergence: 'budget_critical', shouldStop: true, stopReason: 'x', remainingBudgetPercent: 2, iteration: 3, elapsedS: 9 })
    const status = store.getState().getStatus('pipe-1')!
    expect(status.convergence).toBe('budget_critical')
    expect(status.iteration).toBe(3)
    expect(Object.keys(store.getState().statusByPipeline).length).toBe(1)
  })
})
