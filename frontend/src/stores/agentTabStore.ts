/**
 * Agent Tab 状态管理 Store
 *
 * 管理多 Agent Tab 的状态，支持：
 * - 添加/移除 Agent Tab
 * - 切换活跃 Tab
 * - 更新 Tab 状态
 * - 每个 Tab 独立的消息列表
 * - localStorage 持久化（按会话存储标签状态）
 */

import { create } from 'zustand'
import type { AgentTab } from '@/types/task'

/** localStorage 存储键前缀 */
const STORAGE_KEY_PREFIX = 'agent-tabs-'

/**
 * 获取会话对应的存储键
 */
function getStorageKey(sessionId: string): string {
  return `${STORAGE_KEY_PREFIX}${sessionId}`
}

/**
 * 保存标签状态到 localStorage
 */
function saveTabsToStorage(sessionId: string, tabs: AgentTab[], activeTabId: string | null): void {
  try {
    const data = { tabs, activeTabId, savedAt: Date.now() }
    localStorage.setItem(getStorageKey(sessionId), JSON.stringify(data))
  } catch (e) {
    console.warn('[AgentTabStore] 保存标签状态失败:', e)
  }
}

/**
 * 从 localStorage 加载标签状态
 */
function loadTabsFromStorage(
  sessionId: string,
): { tabs: AgentTab[]; activeTabId: string | null } | null {
  try {
    const raw = localStorage.getItem(getStorageKey(sessionId))
    if (!raw) return null
    const data = JSON.parse(raw)
    // 24小时过期
    if (Date.now() - data.savedAt > 24 * 60 * 60 * 1000) {
      localStorage.removeItem(getStorageKey(sessionId))
      return null
    }
    return { tabs: data.tabs || [], activeTabId: data.activeTabId || null }
  } catch (e) {
    console.warn('[AgentTabStore] 加载标签状态失败:', e)
    return null
  }
}

/**
 * Agent Tab 状态接口
 */
interface AgentTabState {
  /** Agent Tab 列表 */
  tabs: AgentTab[]
  /** 当前活跃的 Tab ID */
  activeTabId: string | null
  /** 每个 Tab 的消息映射 (tabId -> messages) */
  tabMessages: Record<string, any[]>
  /** 每个 Tab 的未读消息计数 (tabId -> count) */
  unreadCounts: Record<string, number>
  /** 当前会话 ID（用于持久化标识） */
  currentSessionId: string | null

  /** 添加 Agent Tab */
  addTab: (tab: Omit<AgentTab, 'messages'>) => void
  /** 移除 Agent Tab */
  removeTab: (tabId: string) => void
  /** 设置活跃 Tab */
  setActiveTab: (tabId: string) => void
  /** 更新 Tab 状态 */
  updateTabStatus: (tabId: string, status: AgentTab['status']) => void
  /** 更新 Tab 未读状态 */
  updateTabUnread: (tabId: string, hasUnread: boolean) => void
  /** 添加消息到指定 Tab */
  addMessageToTab: (tabId: string, message: any) => void
  /** 获取当前活跃 Tab 的消息 */
  getActiveTabMessages: () => any[]
  /** 获取当前活跃 Tab */
  getActiveTab: () => AgentTab | null
  /** 清除 Tab 未读计数 */
  clearTabUnread: (tabId: string) => void
  /** 重置所有 Tabs（会话切换时使用） */
  resetAllTabs: () => void
  /** 初始化/切换会话标签（从 localStorage 恢复） */
  initSessionTabs: (sessionId: string) => void
  /** 保存当前标签状态到 localStorage */
  saveCurrentTabs: () => void
  /** 打开子 Tab */
  openSubTab: (tab: Omit<AgentTab, 'messages'>) => void
  /** 关闭 Tab（增强版，支持主 Tab 保护） */
  closeTab: (tabId: string) => void
  /** 切换到 Tab（增强版，自动清除未读） */
  switchToTab: (tabId: string) => void
  /** 标记 Tab 完成 */
  markTabComplete: (tabId: string) => void
  /** 合并到主 Tab（子 Tab 完成后） */
  mergeToMainTab: (subTabId: string) => void
  /** 清除未读（别名） */
  clearUnread: (tabId: string) => void
  /** 更新 Tab 状态（别名） */
  updateTab: (tabId: string, updates: Partial<AgentTab>) => void
  /** 打开子 Agent Tab（统一接口） */
  openSubAgentTab: (params: {
    agentId: string
    agentName: string
    parentRecordId: string
    agentLevel?: 1 | 2 | 3
    taskId?: string
    status?: AgentTab['status']
    setActive?: boolean
  }) => void
}

/**
 * Agent Tab Store
 */
export const useAgentTabStore = create<AgentTabState>((set, get) => ({
  tabs: [],
  activeTabId: null,
  tabMessages: {},
  unreadCounts: {},
  currentSessionId: null,

  /**
   * 初始化/切换会话标签（从 localStorage 恢复）
   */
  initSessionTabs: (sessionId) => {
    const saved = loadTabsFromStorage(sessionId)
    if (saved) {
      const mainTab = saved.tabs.find((t) => t.agentLevel === 1)
      set({
        currentSessionId: sessionId,
        tabs: saved.tabs,
        activeTabId: mainTab?.id || saved.activeTabId || null,
        tabMessages: {},
        unreadCounts: {},
      })
    } else {
      set({
        currentSessionId: sessionId,
        tabs: [],
        activeTabId: null,
        tabMessages: {},
        unreadCounts: {},
      })
    }
  },

  /**
   * 保存当前标签状态到 localStorage
   */
  saveCurrentTabs: () => {
    const { currentSessionId, tabs, activeTabId } = get()
    if (currentSessionId) {
      saveTabsToStorage(currentSessionId, tabs, activeTabId)
    }
  },

  /**
   * 添加 Agent Tab
   */
  addTab: (tabData) => {
    set((state) => {
      const existingTab = state.tabs.find((t) => t.id === tabData.id)

      if (existingTab) {
        return {
          tabs: state.tabs.map((t) => (t.id === tabData.id ? { ...t, ...tabData } : t)),
        }
      }

      const newTab: AgentTab = {
        ...tabData,
        messages: [],
      }

      return {
        tabs: [...state.tabs, newTab],
        tabMessages: {
          ...state.tabMessages,
          [tabData.id]: [],
        },
        unreadCounts: {
          ...state.unreadCounts,
          [tabData.id]: 0,
        },
      }
    })
    get().saveCurrentTabs()
  },

  /**
   * 移除 Agent Tab
   */
  removeTab: (tabId) => {
    set((state) => {
      const newTabs = state.tabs.filter((t) => t.id !== tabId)
      const newTabMessages = { ...state.tabMessages }
      const newUnreadCounts = { ...state.unreadCounts }

      delete newTabMessages[tabId]
      delete newUnreadCounts[tabId]

      let newActiveTabId = state.activeTabId
      if (state.activeTabId === tabId) {
        const mainTab = newTabs.find((t) => t.agentLevel === 1)
        newActiveTabId = mainTab?.id || newTabs[0]?.id || null
      }

      return {
        tabs: newTabs,
        activeTabId: newActiveTabId,
        tabMessages: newTabMessages,
        unreadCounts: newUnreadCounts,
      }
    })
    get().saveCurrentTabs()
  },

  /**
   * 设置活跃 Tab
   */
  setActiveTab: (tabId) => {
    set({
      activeTabId: tabId,
    })

    get().clearTabUnread(tabId)
    get().saveCurrentTabs()
  },

  /**
   * 更新 Tab 状态
   */
  updateTabStatus: (tabId, status) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status } : t)),
    }))
  },

  /**
   * 更新 Tab 未读状态
   */
  updateTabUnread: (tabId, hasUnread) => {
    set((state) => {
      const currentCount = state.unreadCounts[tabId] || 0
      const newCount = hasUnread ? currentCount + 1 : 0

      return {
        tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, hasUnread } : t)),
        unreadCounts: {
          ...state.unreadCounts,
          [tabId]: newCount,
        },
      }
    })
  },

  /**
   * 添加消息到指定 Tab
   */
  addMessageToTab: (tabId, message) => {
    set((state) => {
      const tabMessages = state.tabMessages[tabId] || []
      const existingIndex = tabMessages.findIndex((m) => m.id === message.id)
      let updatedMessages

      if (existingIndex >= 0) {
        updatedMessages = [...tabMessages]
        updatedMessages[existingIndex] = message
      } else {
        updatedMessages = [...tabMessages, message]

        if (state.activeTabId !== tabId) {
          const currentCount = state.unreadCounts[tabId] || 0
          return {
            tabMessages: {
              ...state.tabMessages,
              [tabId]: updatedMessages,
            },
            unreadCounts: {
              ...state.unreadCounts,
              [tabId]: currentCount + 1,
            },
            tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, hasUnread: true } : t)),
          }
        }
      }

      return {
        tabMessages: {
          ...state.tabMessages,
          [tabId]: updatedMessages,
        },
      }
    })
  },

  /**
   * 获取当前活跃 Tab 的消息
   */
  getActiveTabMessages: () => {
    const { activeTabId, tabMessages } = get()
    if (!activeTabId) return []
    return tabMessages[activeTabId] || []
  },

  /**
   * 获取当前活跃 Tab
   */
  getActiveTab: () => {
    const { tabs, activeTabId } = get()
    return tabs.find((t) => t.id === activeTabId) || null
  },

  /**
   * 清除 Tab 未读计数
   */
  clearTabUnread: (tabId) => {
    set({
      unreadCounts: {
        ...get().unreadCounts,
        [tabId]: 0,
      },
      tabs: get().tabs.map((t) => (t.id === tabId ? { ...t, hasUnread: false } : t)),
    })
  },

  /**
   * 重置所有 Tabs（会话切换时使用）
   */
  resetAllTabs: () => {
    set({
      tabs: [],
      activeTabId: null,
      tabMessages: {},
      unreadCounts: {},
    })
  },

  /**
   * 打开子 Tab
   */
  openSubTab: (tabData) => {
    set((state) => {
      const existingTab = state.tabs.find((t) => t.id === tabData.id)

      if (existingTab) {
        return {
          tabs: state.tabs.map((t) => (t.id === tabData.id ? { ...t, ...tabData } : t)),
        }
      }

      const newTab: AgentTab = {
        ...tabData,
        messages: [],
        canClose: true,
      }

      return {
        tabs: [...state.tabs, newTab],
        tabMessages: {
          ...state.tabMessages,
          [tabData.id]: [],
        },
        unreadCounts: {
          ...state.unreadCounts,
          [tabData.id]: 0,
        },
      }
    })
  },

  /**
   * 关闭 Tab（增强版，支持主 Tab 保护）
   */
  closeTab: (tabId) => {
    set((state) => {
      const tab = state.tabs.find((t) => t.id === tabId)
      if (!tab) {
        console.warn(`[AgentTabStore] Tab not found: ${tabId}`)
        return state
      }

      if (!tab.canClose) {
        console.warn(`[AgentTabStore] Cannot close main tab: ${tabId}`)
        return state
      }

      const newTabs = state.tabs.filter((t) => t.id !== tabId)
      const newTabMessages = { ...state.tabMessages }
      const newUnreadCounts = { ...state.unreadCounts }

      delete newTabMessages[tabId]
      delete newUnreadCounts[tabId]

      let newActiveTabId = state.activeTabId
      if (state.activeTabId === tabId) {
        const mainTab = newTabs.find((t) => t.agentLevel === 1)
        newActiveTabId = mainTab?.id || newTabs[0]?.id || null
      }

      return {
        tabs: newTabs,
        activeTabId: newActiveTabId,
        tabMessages: newTabMessages,
        unreadCounts: newUnreadCounts,
      }
    })
    get().saveCurrentTabs()
  },

  /**
   * 切换到 Tab（增强版，自动清除未读）
   */
  switchToTab: (tabId) => {
    const { tabs } = get()
    const tab = tabs.find((t) => t.id === tabId)

    if (!tab) {
      console.warn(`[AgentTabStore] Tab not found: ${tabId}`)
      return
    }

    set({ activeTabId: tabId })
    get().clearTabUnread(tabId)
  },

  /**
   * 标记 Tab 完成
   */
  markTabComplete: (tabId) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status: 'completed' } : t)),
    }))
  },

  /**
   * 合并到主 Tab（子 Tab 完成后）
   */
  mergeToMainTab: (subTabId) => {
    set((state) => {
      const subTab = state.tabs.find((t) => t.id === subTabId)
      const mainTab = state.tabs.find((t) => t.agentLevel === 1)

      if (!subTab || !mainTab) {
        console.warn(`[AgentTabStore] Cannot merge: subTab or mainTab not found`)
        return state
      }

      const subMessages = state.tabMessages[subTabId] || []
      const mainMessages = state.tabMessages[mainTab.id] || []

      const mergedMessages = [
        ...mainMessages,
        ...subMessages.map((msg) => ({
          ...msg,
          metadata: {
            ...msg.metadata,
            mergedFrom: subTabId,
            mergedAt: new Date().toISOString(),
          },
        })),
      ]

      const newTabs = state.tabs.filter((t) => t.id !== subTabId)
      const newTabMessages = { ...state.tabMessages }
      const newUnreadCounts = { ...state.unreadCounts }

      delete newTabMessages[subTabId]
      delete newUnreadCounts[subTabId]

      let newActiveTabId = state.activeTabId
      if (state.activeTabId === subTabId) {
        newActiveTabId = mainTab.id
      }

      return {
        tabs: newTabs,
        activeTabId: newActiveTabId,
        tabMessages: {
          ...newTabMessages,
          [mainTab.id]: mergedMessages,
        },
        unreadCounts: newUnreadCounts,
      }
    })
  },

  /**
   * 清除未读（别名）
   */
  clearUnread: (tabId) => {
    get().clearTabUnread(tabId)
  },

  /**
   * 更新 Tab 状态（别名）
   */
  updateTab: (tabId, updates) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, ...updates } : t)),
    }))
  },

  /**
   * 打开子 Agent Tab（统一接口）
   */
  openSubAgentTab: (params) => {
    const {
      agentId,
      agentName,
      parentRecordId,
      agentLevel = 2,
      taskId,
      status = 'running',
      setActive = true,
    } = params

    set((state) => {
      const tabId = `sub-${parentRecordId}`
      const path = ['主Agent', agentName]

      const existingTab = state.tabs.find((t) => t.id === tabId)

      if (existingTab) {
        return {
          tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status } : t)),
          ...(setActive ? { activeTabId: tabId } : {}),
        }
      }

      const newTab: AgentTab = {
        id: tabId,
        agentId,
        agentName,
        agentLevel,
        taskId,
        parentRecordId,
        path,
        status,
        hasUnread: false,
        canClose: true,
        messages: [],
      }

      return {
        tabs: [...state.tabs, newTab],
        ...(setActive ? { activeTabId: tabId } : {}),
        tabMessages: {
          ...state.tabMessages,
          [tabId]: [],
        },
        unreadCounts: {
          ...state.unreadCounts,
          [tabId]: 0,
        },
      }
    })
    get().saveCurrentTabs()
  },
}))
