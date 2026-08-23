/**
 * TanStack Query 持久化 persister（IndexedDB）
 *
 * 把 query cache 落 IndexedDB，页面刷新后恢复缓存实现「秒开 + 后台刷新」。
 * 与消息缓存（utils/indexedDbStorage.ts）共用 app-db 库但 key 独立，互不影响。
 *
 * 容错语义：IndexedDB 写失败（隐私模式/配额）时静默降级为本次会话无持久化，
 * 不阻断业务（刷新后回到无缓存首次加载，等同消息缓存的内存降级策略）。
 */

import { createAsyncStoragePersister } from '@tanstack/query-async-storage-persister'
import { get, set, del, createStore } from 'idb-keyval'

const idbStore = createStore('app-db', 'kv')

async function getItem(key: string): Promise<string | null> {
  try {
    return (await get<string>(key, idbStore)) ?? null
  } catch {
    return null
  }
}

async function setItem(key: string, value: string): Promise<void> {
  try {
    await set(key, value, idbStore)
  } catch {
    // 静默降级：持久化失败仅丢失刷新后的缓存秒开，不影响运行时数据
  }
}

async function removeItem(key: string): Promise<void> {
  try {
    await del(key, idbStore)
  } catch {
    // 同上，删除失败无业务影响
  }
}

/** 导出供容错单测：IndexedDB 不可用时三操作均不抛、不阻断业务 */
export const queryPersisterStorage = { getItem, setItem, removeItem }

export const queryPersister = createAsyncStoragePersister({
  key: 'tanstack-query-cache',
  storage: { getItem, setItem, removeItem },
})
