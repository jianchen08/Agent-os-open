/**
 * 本地存储工具函数（localStorage封装）
 */

import { MAX_FAVORITE_AGENTS, STORAGE_KEYS, type AgentPreferences } from '../constants/storage'

export { MAX_FAVORITE_AGENTS, STORAGE_KEYS, type AgentPreferences }
export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS]

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
      console.error(`存储数据失败 [${key}]:`, error)
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
        console.error(`读取数据失败 [${key}]:`, parseError)
        return null
      }
    } catch (error) {
      console.error(`读取数据失败 [${key}]:`, error)
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
      console.error(`删除数据失败 [${key}]:`, error)
    }
  }

  /**
   * 清空所有存储项
   */
  clear(): void {
    try {
      localStorage.clear()
    } catch (error) {
      console.error('清空存储失败:', error)
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
 * 认证相关存储工具
 */
export const authStorage = {
  /**
   * 保存认证令牌
   * @param token - 访问令牌
   * @param refreshToken - 刷新令牌（可选）
   */
  setTokens(token: string, refreshToken?: string): void {
    storage.setItem(STORAGE_KEYS.ACCESS_TOKEN, token)
    if (refreshToken) {
      storage.setItem(STORAGE_KEYS.REFRESH_TOKEN, refreshToken)
    }
  },

  /**
   * 获取访问令牌
   * @returns 访问令牌或null
   */
  getToken(): string | null {
    return storage.getItem<string>(STORAGE_KEYS.ACCESS_TOKEN)
  },

  /**
   * 获取刷新令牌
   * @returns 刷新令牌或null
   */
  getRefreshToken(): string | null {
    return storage.getItem<string>(STORAGE_KEYS.REFRESH_TOKEN)
  },

  /**
   * 清除所有认证信息
   */
  clearAuth(): void {
    storage.removeItem(STORAGE_KEYS.ACCESS_TOKEN)
    storage.removeItem(STORAGE_KEYS.REFRESH_TOKEN)
    storage.removeItem(STORAGE_KEYS.USER_INFO)
  },

  /**
   * 保存用户信息
   * @param user - 用户信息
   */
  setUser(user: any): void {
    storage.setItem(STORAGE_KEYS.USER_INFO, user)
  },

  /**
   * 获取用户信息
   * @returns 用户信息或null
   */
  getUser<T>(): T | null {
    return storage.getItem<T>(STORAGE_KEYS.USER_INFO)
  },
}

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

/**
 * Agent 偏好设置存储工具
 * Requirements: 13.2, 13.4, 13.5
 */
export const agentStorage = {
  /**
   * 获取 Agent 偏好设置
   * @returns Agent 偏好设置或默认值
   */
  getPreferences(): AgentPreferences {
    const stored = storage.getItem<AgentPreferences>(STORAGE_KEYS.AGENT_PREFERENCES)
    if (stored === null) {
      return {
        defaultAgentId: null,
        favoriteAgentIds: [],
      }
    }
    return stored
  },

  /**
   * 保存 Agent 偏好设置
   * @param preferences - Agent 偏好设置
   */
  setPreferences(preferences: AgentPreferences): void {
    // 确保常用 Agent 列表不超过最大数量
    const limitedPreferences: AgentPreferences = {
      ...preferences,
      favoriteAgentIds: preferences.favoriteAgentIds.slice(0, MAX_FAVORITE_AGENTS),
    }
    storage.setItem(STORAGE_KEYS.AGENT_PREFERENCES, limitedPreferences)
  },

  /**
   * 获取默认 Agent ID
   * @returns 默认 Agent ID 或 null
   */
  getDefaultAgentId(): string | null {
    return this.getPreferences().defaultAgentId
  },

  /**
   * 设置默认 Agent ID
   * @param agentId - Agent ID
   */
  setDefaultAgentId(agentId: string | null): void {
    const preferences = this.getPreferences()
    this.setPreferences({
      ...preferences,
      defaultAgentId: agentId,
    })
  },

  /**
   * 获取常用 Agent ID 列表
   * @returns 常用 Agent ID 列表
   */
  getFavoriteAgentIds(): string[] {
    return this.getPreferences().favoriteAgentIds
  },

  /**
   * 添加常用 Agent
   * Requirements: 13.5 - 最多 10 个
   * @param agentId - Agent ID
   * @returns 是否添加成功
   */
  addFavoriteAgent(agentId: string): boolean {
    const preferences = this.getPreferences()

    // 检查是否已存在
    if (preferences.favoriteAgentIds.includes(agentId)) {
      return false
    }

    // 检查是否超过最大数量
    if (preferences.favoriteAgentIds.length >= MAX_FAVORITE_AGENTS) {
      return false
    }

    this.setPreferences({
      ...preferences,
      favoriteAgentIds: [...preferences.favoriteAgentIds, agentId],
    })
    return true
  },

  /**
   * 移除常用 Agent
   * @param agentId - Agent ID
   */
  removeFavoriteAgent(agentId: string): void {
    const preferences = this.getPreferences()
    this.setPreferences({
      ...preferences,
      favoriteAgentIds: preferences.favoriteAgentIds.filter((id) => id !== agentId),
    })
  },

  /**
   * 检查是否为常用 Agent
   * @param agentId - Agent ID
   * @returns 是否为常用 Agent
   */
  isFavoriteAgent(agentId: string): boolean {
    return this.getPreferences().favoriteAgentIds.includes(agentId)
  },

  /**
   * 清除 Agent 偏好设置
   */
  clearPreferences(): void {
    storage.removeItem(STORAGE_KEYS.AGENT_PREFERENCES)
  },
}
