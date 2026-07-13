/**
 * IndexedDB 持久化 storage adapter
 *
 * 为 zustand persist 提供 StateStorage（getItem/setItem/removeItem），
 * 数据落 IndexedDB（GB 级容量、异步不阻塞 UI），替代 localStorage 存大体积消息缓存。
 *
 * 设计要点：
 * - 复用 persistThrottle 节流：流式高频写入合并为最后一次落盘。
 * - 容错降级：IndexedDB 不可用（隐私模式 / Safari 极端限制）时回退到内存 Map，
 *   业务不阻断（语义等同 localStorage 配额耗尽的容错：丢失仅限本次会话，刷新后从 API 重载）。
 */

import { get, set, del, createStore } from 'idb-keyval'
import { createJSONStorage } from 'zustand/middleware'
import { createPersistThrottle } from './persistThrottle'

/** 复用的 IndexedDB keyval store 句柄（库名 / store 名固定） */
const idbStore = createStore('app-db', 'kv')

/** 内存降级存储：IndexedDB 不可用时的回退（仅本次会话有效，刷新丢失） */
const memoryFallback = new Map<string, string>()
let useMemoryFallback = false

/** 标记并返回是否已进入内存降级模式（首次失败时记一次日志） */
function markMemoryFallback(): void {
  if (useMemoryFallback) return
  useMemoryFallback = true
  console.warn(
    '[indexedDbStorage] IndexedDB 不可用，消息缓存降级为内存模式（刷新后从 API 重新加载）',
  )
}

/** 容错 get：IndexedDB 失败则读内存降级 */
async function safeGet(name: string): Promise<string | null> {
  if (useMemoryFallback) return memoryFallback.get(name) ?? null
  try {
    const val = await get<string>(name, idbStore)
    return val ?? null
  } catch {
    markMemoryFallback()
    return memoryFallback.get(name) ?? null
  }
}

/** 容错 set：IndexedDB 失败则写内存降级 */
async function safeSet(name: string, value: string): Promise<void> {
  if (useMemoryFallback) {
    memoryFallback.set(name, value)
    return
  }
  try {
    await set(name, value, idbStore)
  } catch {
    markMemoryFallback()
    memoryFallback.set(name, value)
  }
}

/** 容错 del：IndexedDB 失败则删内存降级 */
async function safeDel(name: string): Promise<void> {
  if (useMemoryFallback) {
    memoryFallback.delete(name)
    return
  }
  try {
    await del(name, idbStore)
  } catch {
    markMemoryFallback()
    memoryFallback.delete(name)
  }
}

/** 节流器：窗口内多次 setItem 合并为最后一次落盘（流式高频写入合并） */
const throttle = createPersistThrottle((name, value) => safeSet(name, value))

/**
 * zustand persist 可用的 StateStorage（经 createJSONStorage 包装，
 * 自动处理 JSON 序列化 / 反序列化）。
 *
 * - getItem：异步读取，失败降级内存。
 * - setItem：走节流（trailing 合并），避免流式逐次落盘。
 * - removeItem：取消挂起的节流写入后删除，防止 remove 后又被 trailing 写回。
 */
export const indexedDbStorage = createJSONStorage(() => ({
  getItem: (name) => safeGet(name),
  setItem: (name, value) => {
    throttle.schedule(name, value)
  },
  removeItem: (name) => {
    throttle.cancel()
    void safeDel(name)
  },
}))

/** 供测试 / 显式清理使用：强制落盘当前缓冲的挂起写入 */
export function flushIndexedDbPersist(): void {
  throttle.flush()
}
