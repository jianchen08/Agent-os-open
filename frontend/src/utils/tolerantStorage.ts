/** zustand persist 容错 storage 工厂 */

import { createJSONStorage, type StateStorage } from 'zustand/middleware'
import { loggers } from '@/utils/logger'

const logger = loggers.storage

/** 创建一个容错的 localStorage 包装，供 zustand persist 使用。 - getItem / removeItem：失败返回 null / 静默（存储被禁用或锁定时） */
export function createTolerantStorage() {
  let quotaWarned = false
  const storage: StateStorage = {
    getItem: (name) => {
      try {
        return window.localStorage.getItem(name)
      } catch {
        return null
      }
    },
    setItem: (name, value) => {
      try {
        window.localStorage.setItem(name, value)
      } catch (err) {
        // 配额满或禁用：仅记录一次 warn，避免每次 set 都刷屏
        if (!quotaWarned) {
          quotaWarned = true
          logger.warn(
            '[persist] 持久化失败（localStorage 配额耗尽或不可用），'
            + '本次会话内状态仅保存在内存，刷新后将丢失此 key=%s: err=%s',
            name, err,
          )
        }
      }
    },
    removeItem: (name) => {
      try {
        window.localStorage.removeItem(name)
      } catch {
        /* 忽略清理失败 */
      }
    },
  }
  return createJSONStorage(() => storage)
}
