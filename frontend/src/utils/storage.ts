/**
 * 本地存储工具函数（localStorage封装）
 */

import { loggers } from './logger'
import { STORAGE_KEYS } from '../constants/storage'

export { STORAGE_KEYS }
export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS]

/** 存储失败告警只发一次（对齐 tolerantStorage 约定）：配额满/禁用时每次 set/get
 *  都告警会刷屏，但完全静默会让「状态未持久化」不可察觉。 */
let storageWarned = false
function warnStorageOnce(message: string, detail: unknown): void {
  if (storageWarned) return
  storageWarned = true
  loggers.storage.warn('[storage] %s detail=%o', message, detail)
}

/**
 * 存储服务类
 */
class StorageService {
  /**
   * 设置存储项
   * @param key - 键名
   * @param value - 值（会自动序列化为JSON）
   */
  setItem<T>(key: string, value: T): void {
    try {
      // 防止 JSON.stringify(undefined) 产生无效字符串 "undefined"
      if (value === undefined) {
        localStorage.removeItem(key)
        return
      }
      const serializedValue = JSON.stringify(value)
      localStorage.setItem(key, serializedValue)
    } catch (error) {
      warnStorageOnce(`存储数据失败 [${key}]`, error)
    }
  }

  /**
   * 获取存储项
   * @param key - 键名
   * @returns 值（会自动反序列化）或null
   */
  getItem<T>(key: string): T | null {
    try {
      const serializedValue = localStorage.getItem(key)
      if (serializedValue === null) {
        return null
      }

      // 预检查：处理 localStorage 中存储了无效值的情况
      if (
        serializedValue === 'undefined' ||
        serializedValue === 'null' ||
        serializedValue === 'NaN'
      ) {
        localStorage.removeItem(key)
        return null
      }

      // 尝试解析JSON
      try {
        return JSON.parse(serializedValue) as T
      } catch (parseError) {
        // 如果JSON.parse失败,可能是简单的字符串值(如"system", "light", "dark", "true", "false")
        // 尝试直接返回字符串值或转换为布尔值
        if (
          serializedValue === 'system' ||
          serializedValue === 'light' ||
          serializedValue === 'dark'
        ) {
          return serializedValue as T
        }
        if (serializedValue === 'true') {
          return true as T
        }
        if (serializedValue === 'false') {
          return false as T
        }
        warnStorageOnce(`解析数据失败 [${key}]`, parseError)
        return null
      }
    } catch (error) {
      warnStorageOnce(`读取数据失败 [${key}]`, error)
      return null
    }
  }

  /**
   * 移除存储项
   * @param key - 键名
   */
  removeItem(key: string): void {
    try {
      localStorage.removeItem(key)
    } catch (error) {
      warnStorageOnce(`删除数据失败 [${key}]`, error)
    }
  }

  /**
   * 清空所有存储项
   */
  clear(): void {
    try {
      localStorage.clear()
    } catch (error) {
      warnStorageOnce('清空存储失败', error)
    }
  }

  /**
   * 检查键是否存在
   * @param key - 键名
   * @returns 是否存在
   */
  hasItem(key: string): boolean {
    return localStorage.getItem(key) !== null
  }

  /**
   * 获取所有键名
   * @returns 键名数组
   */
  getAllKeys(): string[] {
    const keys: string[] = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key) {
        keys.push(key)
      }
    }
    return keys
  }

  /**
   * 获取存储大小（字节）
   * @returns 存储大小
   */
  getSize(): number {
    let size = 0
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (key) {
        const value = localStorage.getItem(key)
        if (value) {
          size += key.length + value.length
        }
      }
    }
    return size
  }
}

/**
 * 导出存储服务单例
 */
export const storage = new StorageService()

/**
 * UI相关存储工具
 */
export const uiStorage = {
  /**
   * 保存主题设置
   * @param theme - 主题（light/dark）
   */
  setTheme(theme: 'light' | 'dark'): void {
    storage.setItem(STORAGE_KEYS.THEME, theme)
  },

  /**
   * 获取主题设置
   * @returns 主题或null
   */
  getTheme(): 'light' | 'dark' | null {
    return storage.getItem<'light' | 'dark'>(STORAGE_KEYS.THEME)
  },

  /**
   * 保存侧边栏状态
   * @param collapsed - 是否折叠
   */
  setSidebarCollapsed(collapsed: boolean): void {
    storage.setItem(STORAGE_KEYS.SIDEBAR_COLLAPSED, collapsed)
  },

  /**
   * 获取侧边栏状态
   * @returns 是否折叠或null
   */
  getSidebarCollapsed(): boolean | null {
    return storage.getItem<boolean>(STORAGE_KEYS.SIDEBAR_COLLAPSED)
  },

  /** 保存侧边栏宽度比例（0~1） */
  setSidebarRatio(ratio: number | undefined): void {
    storage.setItem(STORAGE_KEYS.SIDEBAR_RATIO, ratio)
  },

  /**
   * 获取侧边栏宽度比例
   * @returns 比例（0~1）或 null
   */
  getSidebarRatio(): number | null {
    const ratio = storage.getItem<number>(STORAGE_KEYS.SIDEBAR_RATIO)
    if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio <= 0 || ratio >= 1) {
      return null
    }
    return ratio
  },

  /**
   * 保存最后活跃会话ID
   * @param sessionId - 会话ID
   */
  setLastActiveSession(sessionId: string): void {
    storage.setItem(STORAGE_KEYS.LAST_ACTIVE_SESSION, sessionId)
  },

  /**
   * 获取最后活跃会话ID
   * @returns 会话ID或null
   */
  getLastActiveSession(): string | null {
    return storage.getItem<string>(STORAGE_KEYS.LAST_ACTIVE_SESSION)
  },

  /**
   * 保存任务状态面板状态
   * @param collapsed - 是否折叠
   */
  setTaskPanelCollapsed(collapsed: boolean): void {
    storage.setItem(STORAGE_KEYS.TASK_PANEL_COLLAPSED, collapsed)
  },

  /**
   * 获取任务状态面板状态
   * @returns 是否折叠或null
   */
  getTaskPanelCollapsed(): boolean | null {
    return storage.getItem<boolean>(STORAGE_KEYS.TASK_PANEL_COLLAPSED)
  },

  /**
   * 保存工作区面板状态
   * @param collapsed - 是否折叠
   */
  setWorkspaceCollapsed(collapsed: boolean): void {
    storage.setItem(STORAGE_KEYS.WORKSPACE_COLLAPSED, collapsed)
  },

  /**
   * 获取工作区面板状态
   * @returns 是否折叠或null
   */
  getWorkspaceCollapsed(): boolean | null {
    return storage.getItem<boolean>(STORAGE_KEYS.WORKSPACE_COLLAPSED)
  },

  /**
   * 保存工作区面板宽度比例（传 undefined 会清除记录）
   * @param ratio - 比例（0~1，相对 splitter 容器）
   */
  setWorkspacePanelRatio(ratio: number | undefined): void {
    storage.setItem(STORAGE_KEYS.WORKSPACE_PANEL_RATIO, ratio)
  },

  /**
   * 获取工作区面板宽度比例
   * @returns 比例（0~1）或null（非法或未设置）
   */
  getWorkspacePanelRatio(): number | null {
    const ratio = storage.getItem<number>(STORAGE_KEYS.WORKSPACE_PANEL_RATIO)
    if (typeof ratio !== 'number' || !Number.isFinite(ratio) || ratio <= 0 || ratio >= 1) {
      return null
    }
    return ratio
  },

  /**
   * 保存思考模式启用状态
   * @param enabled - 是否启用
   */
  setThinkingModeEnabled(enabled: boolean): void {
    storage.setItem(STORAGE_KEYS.THINKING_MODE_ENABLED, enabled)
  },

  /**
   * 获取思考模式启用状态
   * @returns 是否启用或null
   */
  getThinkingModeEnabled(): boolean | null {
    return storage.getItem<boolean>(STORAGE_KEYS.THINKING_MODE_ENABLED)
  },
}
