/**
 * zustand persist 容错 storage 工厂
 *
 * BUG-FIX-fix_20260628_persist_quota_blocks_toggle:
 * 问题根因: zustand persist 的默认 storage 直接调用 localStorage.setItem，
 *   配额满时抛 QuotaExceededError，异常冒泡到 store action（如 toggleMode），
 *   表现为控制台 Uncaught QuotaExceededError 且内存状态也没更新成功。
 *   该问题此前只在 pipelineMessageStore 内修复（tolerantJsonStorage），
 *   其余 persist store（layoutModeStore 等）潜伏同样的崩溃。
 * 修复方案: 抽出共享容错 storage。setItem 失败时吞掉异常、仅记一次 warn，
 *   内存状态照常更新，业务 action 永不抛异常。
 * 影响范围: 所有使用本工厂的低频 UI persist store（布局切换、主题、工作区、长期任务）。
 *   pipelineMessageStore 因高频写入自带节流，沿用其内部实现，不复用本工厂。
 */

import { createJSONStorage, type StateStorage } from 'zustand/middleware'
import { loggers } from '@/utils/logger'

const logger = loggers.storage

/**
 * 创建一个容错的 localStorage 包装，供 zustand persist 使用。
 *
 * - getItem / removeItem：失败返回 null / 静默（存储被禁用或锁定时）
 * - setItem：失败（含 QuotaExceededError）吞掉异常，仅记一次 warn 防刷屏
 *
 * 每次 createTolerantStorage() 调用返回独立实例，warn-once 标志也相互独立。
 */
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
