/**
 * @feature: FP-0.2.用户隔离 | @ci: frontend-test
 * 切户清账单点（隔离执法点②）行为测试
 *
 * 缺陷回归（1018 实证）：admin/maintenance 同机交替登录，旧账户 persist 缓存
 * 先渲染（闪现）再被服务端对账清掉（全空）。契约：账户身份翻转（A→B / 登出）
 * 必须 purge 全部持久化缓存面（zustand persist 存储+内存、query 持久缓存、
 * 用户维 localStorage 键、legacy 裸键），B 永远读不到 A 的缓存。
 * 豁免：同户重登录（previous===next）不 purge——装机版每次启动自动登录场景
 * 缓存秒开语义保持。
 */
import fakeIndexedDB from 'fake-indexeddb'
import { describe, it, expect, beforeEach, vi } from 'vitest'
// 静音 logger（tolerantStorage/indexedDbStorage 降级路径 warn）
vi.mock('@/utils/logger', () => ({
  loggers: {
    storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))
// IndexedDB 面用 fake-indexeddb（queryPersister.test.ts 同款注入）
;(globalThis as { indexedDB?: unknown }).indexedDB = fakeIndexedDB

const { transitionCacheUser } = await import('@/services/auth/cacheBoundary')
const { setCacheUserId } = await import('@/utils/userScopedStorage')
const { usePipelineMessageStore } = await import('@/stores/pipelineMessageStore')
const { useChatInputStore } = await import('@/stores/chatInputStore')
const { useLongTermTaskStore } = await import('@/stores/longTermTaskStore')

/** 等待 IndexedDB 派生微任务落定（removeItem/clearStorage fire-and-forget 面） */
const flushIdb = () => new Promise<void>((r) => setTimeout(r, 0))

async function idbKeys(): Promise<string[]> {
  const { createStore, entries } = await import('idb-keyval')
  const store = createStore('app-db', 'kv')
  return (await entries(store)).map(([k]) => String(k))
}

describe('cacheBoundary 切户清账', () => {
  beforeEach(async () => {
    localStorage.clear()
    setCacheUserId(null)
    // 每例干净 IndexedDB（fake-indexeddb 全局实例逐例重置不可行，直清键）
    for (const k of await idbKeys()) {
      const { del, createStore } = await import('idb-keyval')
      await del(k, createStore('app-db', 'kv'))
    }
    vi.resetModules()
  })

  it('账户翻转 A→B：A 的消息缓存对 B 不可见（内存+存储双侧）', async () => {
    setCacheUserId('u-aaa')
    // A 名下写入消息缓存（走真实 persist 面 → IndexedDB scoped 键）
    usePipelineMessageStore.setState((s) => ({
      messagesByPipeline: { ...s.messagesByPipeline, 'pipe-1': [] },
    }))
    usePipelineMessageStore.getState().addMessage?.('pipe-1', {
      id: 'm1', role: 'user', content: 'admin 的消息', timestamp: new Date().toISOString(),
    } as never)
    await flushIdb()
    expect(Object.keys(usePipelineMessageStore.getState().messagesByPipeline)).toContain('pipe-1')

    // 切户到 B
    transitionCacheUser('u-bbb')
    await flushIdb()

    // 内存面：B 视角消息表为空
    expect(usePipelineMessageStore.getState().messagesByPipeline['pipe-1']).toBeUndefined()
    // 存储面：A 的 scoped 键已清除，B 未读任何 A 数据
    const keys = await idbKeys()
    expect(keys.some((k) => k.includes('u-aaa'))).toBe(false)
  })

  it('同户重入（previous===next）不 purge——装机版自动登录缓存秒开保持', async () => {
    setCacheUserId('u-same')
    useChatInputStore.getState().saveDraft('sess-1', '草稿不丢')
    await flushIdb()

    transitionCacheUser('u-same')
    expect(useChatInputStore.getState().drafts['sess-1']).toBe('草稿不丢')
  })

  it('登出（null）：purge 全部面并落回匿名态', async () => {
    setCacheUserId('u-ccc')
    useLongTermTaskStore.setState({ activeTaskId: 'task-9' })
    localStorage.setItem('last_active_session', 'sess-admin')
    await flushIdb()

    transitionCacheUser(null)
    await flushIdb()

    expect(useLongTermTaskStore.getState().activeTaskId).toBeNull()
    expect(localStorage.getItem('last_active_session')).toBeNull()
  })

  it('purge 覆盖用户维 localStorage 键（含 watchlist——旧账户会话不得并入新账户列表）', () => {
    setCacheUserId('u-ddd')
    localStorage.setItem('session_watchlist', JSON.stringify([{ id: 'sess-admin' }]))
    localStorage.setItem('agent-tabs-sess-admin', '{"tabs":[]}')

    transitionCacheUser('u-eee')

    expect(localStorage.getItem('session_watchlist')).toBeNull()
    expect(localStorage.getItem('agent-tabs-sess-admin')).toBeNull()
  })

  it('purge 覆盖 legacy 裸键（本修复前已落盘的旧账户缓存，升级即清）', async () => {
    localStorage.setItem('layout-mode', '{"mode":"five-space"}')
    localStorage.setItem('workspace-store', '{}')
    const { set: idbSet, createStore } = await import('idb-keyval')
    await idbSet('pipeline-messages', '{"state":{}}', createStore('app-db', 'kv'))
    await idbSet('tanstack-query-cache', '{"clientState":{}}', createStore('app-db', 'kv'))

    setCacheUserId('u-fff')
    transitionCacheUser('u-0aa')
    await flushIdb()

    expect(localStorage.getItem('layout-mode')).toBeNull()
    expect(localStorage.getItem('workspace-store')).toBeNull()
    const keys = await idbKeys()
    expect(keys).not.toContain('pipeline-messages')
    expect(keys).not.toContain('tanstack-query-cache')
  })

  it('匿名→首登不 purge（无前账户可泄，保留既有面）', () => {
    setCacheUserId(null)
    const spy = vi.spyOn(useChatInputStore.persist, 'clearStorage')
    transitionCacheUser('u-first')
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
