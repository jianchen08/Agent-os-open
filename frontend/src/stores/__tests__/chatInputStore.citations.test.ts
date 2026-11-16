// @feature FP-0.2.评审引用交互 R5：输入框引用卡片化 | @ci: frontend-test
/**
 * chatInputStore 引用条目测试（2026-11-05 R5 卡片化）。
 *
 * 契约：
 *  - citationsByDraftKey 按 draftKey 隔离（与 drafts 同键域同机制）；
 *  - addCitation 入口统一截断 excerpt（400 字 + 省略号，与 markdown 组装真源
 *    citationCompose 同阈值——保证发送装配产物与旧 requestInsert 通道等价）；
 *  - id/createdAt 缺省由 store 补齐（显式值优先）；
 *  - removeCitation 精确移除目标条目；clearCitations 清整列；
 *  - citationsByDraftKey 随 persist 落盘（partialize）且刷新可往返。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { userScopedKey } from '@/utils/userScopedStorage'
import type * as chatInputStoreMod from '@/stores/chatInputStore'

vi.mock('@/utils/logger', () => ({
  loggers: { storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

const msgItem = (over: Partial<chatInputStoreMod.CitationItem> = {}) => ({
  id: 'c1',
  kind: 'message' as const,
  title: '用户',
  excerpt: '被引用的文字',
  createdAt: 1000,
  ...over,
})

describe('chatInputStore - 引用条目（R5 卡片化）', () => {
  let store: chatInputStoreMod.useChatInputStore

  beforeEach(async () => {
    localStorage.clear()
    vi.resetModules()
    store = (await import('@/stores/chatInputStore')).useChatInputStore
    store.setState({ citationsByDraftKey: {} })
  })

  it('addCitation 入列；draftKey 隔离（两组有区分度输入互不可见）', () => {
    store.getState().addCitation('key-a', msgItem({ id: 'a1', excerpt: '甲会话引用' }))
    store.getState().addCitation('key-b', msgItem({ id: 'b1', kind: 'code', title: 'src/main.py:L2', excerpt: 'line2' }))

    expect(store.getState().citationsByDraftKey['key-a']?.map((c) => c.id)).toEqual(['a1'])
    expect(store.getState().citationsByDraftKey['key-b']?.map((c) => c.id)).toEqual(['b1'])
    expect(store.getState().citationsByDraftKey['key-a']?.[0].excerpt).toBe('甲会话引用')
    expect(store.getState().citationsByDraftKey['key-b']?.[0].kind).toBe('code')
  })

  it('removeCitation 精确移除目标 id，余序不变；列空后键删除', () => {
    store.getState().addCitation('k', msgItem({ id: 'x1', excerpt: '第一条' }))
    store.getState().addCitation('k', msgItem({ id: 'x2', excerpt: '第二条' }))
    store.getState().addCitation('k', msgItem({ id: 'x3', excerpt: '第三条' }))

    store.getState().removeCitation('k', 'x2')
    expect(store.getState().citationsByDraftKey['k']?.map((c) => c.id)).toEqual(['x1', 'x3'])

    store.getState().removeCitation('k', 'x1')
    store.getState().removeCitation('k', 'x3')
    expect(store.getState().citationsByDraftKey['k']).toBeUndefined()
  })

  it('clearCitations 清目标 key 整列，其他 key 不动', () => {
    store.getState().addCitation('k1', msgItem({ id: 'm1' }))
    store.getState().addCitation('k2', msgItem({ id: 'm2' }))

    store.getState().clearCitations('k1')
    expect(store.getState().citationsByDraftKey['k1']).toBeUndefined()
    expect(store.getState().citationsByDraftKey['k2']?.map((c) => c.id)).toEqual(['m2'])
  })

  it('excerpt 入口统一截断：超 400 字取前 400 加省略号；短文本原样', () => {
    store.getState().addCitation('k', msgItem({ id: 'long', excerpt: 'a'.repeat(500) }))
    store.getState().addCitation('k', msgItem({ id: 'short', excerpt: '短摘录' }))

    const list = store.getState().citationsByDraftKey['k'] ?? []
    expect(list.find((c) => c.id === 'long')?.excerpt).toBe(`${'a'.repeat(400)}…`)
    expect(list.find((c) => c.id === 'short')?.excerpt).toBe('短摘录')
  })

  it('id/createdAt 缺省由 store 补齐；显式非零值优先', () => {
    store.getState().addCitation('k', {
      kind: 'message',
      title: '用户',
      excerpt: '缺省 id 条目',
      createdAt: 1710000000000, // 显式时刻优先保留
    })
    const first = (store.getState().citationsByDraftKey['k'] ?? [])[0]
    expect(first.id).toBeTruthy()
    expect(first.createdAt).toBe(1710000000000)

    store.getState().addCitation('k', {
      kind: 'message',
      title: '用户',
      excerpt: '全缺省条目',
    })
    const second = (store.getState().citationsByDraftKey['k'] ?? [])[1]
    expect(second.id).toBeTruthy()
    expect(second.id).not.toBe(first.id)
    expect(second.createdAt).toBeGreaterThan(0)
  })

  it('citationsByDraftKey 随 persist 落盘且重载往返；瞬态 pendingInsert 不落盘', async () => {
    store.getState().addCitation('persist-k', msgItem({ id: 'pc1', excerpt: '持久化引用' }))
    store.getState().requestInsert('瞬态桥接')

    const raw = localStorage.getItem(userScopedKey('chat-input-drafts')) || ''
    const parsed = JSON.parse(raw)
    expect(parsed.state.citationsByDraftKey['persist-k']?.[0].excerpt).toBe('持久化引用')
    expect(parsed.state.pendingInsert).toBeUndefined()

    // 往返：重置模块后新实例从 localStorage 再水合
    vi.resetModules()
    const revived = (await import('@/stores/chatInputStore')).useChatInputStore
    expect(revived.getState().citationsByDraftKey['persist-k']?.map((c) => c.id)).toEqual(['pc1'])
  })
})
