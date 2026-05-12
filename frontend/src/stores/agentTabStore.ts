/**
 * Agent Tab 状态管�?Store
 *
 * 管理�?Agent Tab 的状态，支持�?
 * - 添加/移除 Agent Tab
 * - 切换活跃 Tab
 * - 更新 Tab 状�?
 * - 每个 Tab 独立的消息列�?
 * - localStorage 持久化（按会话存储标签状态）
 */

import { create } from 'zustand'
import { getMessages } from '@/services/api/session'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { AgentTab } from '@/types/task'

/** localStorage 存储键前缀 */
const STORAGE_KEY_PREFIX = 'agent-tabs-'
/** 每个�?Tab 缓存�?localStorage 的最大消息条�?*/
const MAX_CACHED_MESSAGES_PER_TAB = 50

/**
 * 获取会话对应的存储键
 */
function getStorageKey(sessionId: string): string {
  return `${STORAGE_KEY_PREFIX}${sessionId}`
}

/**
 * 保存标签状态到 localStorage（包含最�?N 条消息缓存和 pipeline 映射�?
 */
function saveTabsToStorage(
  sessionId: string,
  tabs: AgentTab[],
  activeTabId: string | null,
  tabMessages: Record<string, any[]>,
  pipelineTabMap: Record<string, string>,
): void {
  try {
    // 对每�?Tab 的消息截取最�?N 条，避免 localStorage 超限
    const cachedMessages: Record<string, any[]> = {}
    for (const tabId of Object.keys(tabMessages)) {
      const msgs = tabMessages[tabId] || []
      cachedMessages[tabId] = msgs.slice(-MAX_CACHED_MESSAGES_PER_TAB)
    }
    const data = { tabs, activeTabId, tabMessages: cachedMessages, pipelineTabMap, savedAt: Date.now() }
    localStorage.setItem(getStorageKey(sessionId), JSON.stringify(data))
  } catch (e) {
    console.warn('[AgentTabStore] 保存标签状态失�?', e)
  }
}

/**
 * �?localStorage 加载标签状态（含缓存消息和 pipeline 映射�?
 */
function loadTabsFromStorage(
  sessionId: string,
): { tabs: AgentTab[]; activeTabId: string | null; tabMessages: Record<string, any[]>; pipelineTabMap: Record<string, string> } | null {
  try {
    const raw = localStorage.getItem(getStorageKey(sessionId))
    if (!raw) return null
    const data = JSON.parse(raw)
    // 24小时过期
    if (Date.now() - data.savedAt > 24 * 60 * 60 * 1000) {
      localStorage.removeItem(getStorageKey(sessionId))
      return null
    }
    return {
      tabs: data.tabs || [],
      activeTabId: data.activeTabId || null,
      tabMessages: data.tabMessages || {},
      pipelineTabMap: data.pipelineTabMap || {},
    }
  } catch (e) {
    console.warn('[AgentTabStore] 加载标签状态失�?', e)
    return null
  }
}

/**
 * Agent Tab 状态接�?
 */
interface AgentTabState {
  /** Agent Tab 列表 */
  tabs: AgentTab[]
  /** 当前活跃�?Tab ID */
  activeTabId: string | null
  /** 每个 Tab 的消息映�?(tabId -> messages) */
  tabMessages: Record<string, any[]>
  /** 每个 Tab 的消息加载状态（防止并发重复加载�?*/
  tabMessagesLoading: Record<string, boolean>
  /** 每个 Tab 的未读消息计�?(tabId -> count) */
  unreadCounts: Record<string, number>
  /** 当前会话 ID（用于持久化标识�?*/
  currentSessionId: string | null
  /** pipeline_id �?tabId 映射（用于流式消息路由到对应�?Tab�?*/
  pipelineTabMap: Record<string, string>

  /** 添加 Agent Tab */
  addTab: (tab: Omit<AgentTab, 'messages'>) => void
  /** 移除 Agent Tab */
  removeTab: (tabId: string) => void
  /** 设置活跃 Tab */
  setActiveTab: (tabId: string) => void
  /** 更新 Tab 状�?*/
  updateTabStatus: (tabId: string, status: AgentTab['status']) => void
  /** 更新 Tab 未读状�?*/
  updateTabUnread: (tabId: string, hasUnread: boolean) => void
  /** 添加消息到指�?Tab */
  addMessageToTab: (tabId: string, message: any) => void
  /** 获取当前活跃 Tab 的消�?*/
  getActiveTabMessages: () => any[]
  /** 获取当前活跃 Tab */
  getActiveTab: () => AgentTab | null
  /** 清除 Tab 未读计数 */
  clearTabUnread: (tabId: string) => void
  /** 重置所�?Tabs（会话切换时使用�?*/
  resetAllTabs: () => void
  /** 初始�?切换会话标签（从 localStorage 恢复�?*/
  initSessionTabs: (sessionId: string) => void
  /** 保存当前标签状态到 localStorage */
  saveCurrentTabs: () => void
  /** 打开�?Tab */
  openSubTab: (tab: Omit<AgentTab, 'messages'>) => void
  /** 关闭 Tab（增强版，支持主 Tab 保护�?*/
  closeTab: (tabId: string) => void
  /** 切换�?Tab（增强版，自动清除未读） */
  switchToTab: (tabId: string) => void
  /** 标记 Tab 完成 */
  markTabComplete: (tabId: string) => void
  /** 合并到主 Tab（子 Tab 完成后） */
  mergeToMainTab: (subTabId: string) => void
  /** 清除未读（别名） */
  clearUnread: (tabId: string) => void
  /** 更新 Tab 状态（别名�?*/
  updateTab: (tabId: string, updates: Partial<AgentTab>) => void
  /** 打开�?Agent Tab（统一接口�?*/
  openSubAgentTab: (params: {
    agentId: string
    agentName: string
    parentRecordId: string
    agentLevel?: 1 | 2 | 3
    taskId?: string
    status?: AgentTab['status']
    setActive?: boolean
    /** pipeline_id，用于后续流式消息路由到该子 Tab */
    pipelineId?: string
  }) => void
  /** 注册 pipeline_id �?tabId 映射 */
  registerPipelineTab: (pipelineId: string, tabId: string) => void
  /** 根据 pipeline_id 查找对应�?tabId */
  getTabIdByPipeline: (pipelineId: string) => string | undefined
  /** 从后�?API 加载�?Tab 消息（持久化恢复�?*/
  loadTabMessages: (tabId: string, pipelineRunId?: string) => Promise<void>
}

/**
 * Agent Tab Store
 */
export const useAgentTabStore = create<AgentTabState>((set, get) => ({
  tabs: [],
  activeTabId: null,
  tabMessages: {},
  tabMessagesLoading: {},
  unreadCounts: {},
  currentSessionId: null,
  pipelineTabMap: {},

  /**
   * 初始�?切换会话标签（从 localStorage 恢复，含缓存消息�?pipeline 映射�?
   */
  initSessionTabs: (sessionId) => {
    const saved = loadTabsFromStorage(sessionId)
    if (saved) {
      const mainTab = saved.tabs.find((t) => t.agentLevel === 1)
      set({
        currentSessionId: sessionId,
        tabs: saved.tabs,
        activeTabId: mainTab?.id || saved.activeTabId || null,
        tabMessages: saved.tabMessages || {},
        tabMessagesLoading: {},
        unreadCounts: {},
        pipelineTabMap: saved.pipelineTabMap || {},
      })

      // 后台异步校验：对�?parentRecordId 的子 Tab，静默从 API 刷新消息
      saved.tabs.forEach((tab) => {
        if (tab.parentRecordId && tab.agentLevel !== 1) {
          get().loadTabMessages(tab.id)
        }
      })
    } else {
      const mainTab: AgentTab = {
        id: `main-${sessionId}`,
        agentId: '', agentName: '主Agent', agentLevel: 1,
        taskId: undefined, parentRecordId: undefined, pipelineRunId: undefined,
        path: ['主Agent'], status: 'running', hasUnread: false,
        canClose: false, messages: [],
      }
      set({
        currentSessionId: sessionId,
        tabs: [mainTab],
        activeTabId: mainTab.id,
        tabMessages: {},
        tabMessagesLoading: {},
        unreadCounts: {},
      })
    }
  },

  /**
   * 保存当前标签状态到 localStorage（含消息缓存�?pipeline 映射�?
   */
  saveCurrentTabs: () => {
    const { currentSessionId, tabs, activeTabId, tabMessages, pipelineTabMap } = get()
    if (currentSessionId) {
      saveTabsToStorage(currentSessionId, tabs, activeTabId, tabMessages, pipelineTabMap)
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
   * 设置活跃 Tab（子 Tab 时自动从后端加载消息�?
   */
  setActiveTab: (tabId) => {
    set({
      activeTabId: tabId,
    })

    get().clearTabUnread(tabId)
    get().saveCurrentTabs()

    // �?Tab 切换时触发消息持久化加载
    const tab = get().tabs.find((t) => t.id === tabId)
    if (tab && tab.parentRecordId && tab.agentLevel !== 1) {
      get().loadTabMessages(tabId)
    }
  },

  /**
   * 更新 Tab 状�?
   */
  updateTabStatus: (tabId, status) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status } : t)),
    }))
  },

  /**
   * 更新 Tab 未读状�?
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
   * 添加消息到指�?Tab
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
   * 获取当前活跃 Tab 的消�?
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
   * 重置所�?Tabs（会话切换时使用�?
   */
  resetAllTabs: () => {
    set({
      tabs: [],
      activeTabId: null,
      tabMessages: {},
      tabMessagesLoading: {},
      unreadCounts: {},
      pipelineTabMap: {},
    })
  },

  /**
   * 打开�?Tab
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
   * 关闭 Tab（增强版，支持主 Tab 保护，同时清�?pipeline 映射�?
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

      // 清理指向�?Tab �?pipeline 映射
      const newPipelineTabMap = { ...state.pipelineTabMap }
      for (const [pid, tid] of Object.entries(newPipelineTabMap)) {
        if (tid === tabId) {
          delete newPipelineTabMap[pid]
        }
      }

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
        pipelineTabMap: newPipelineTabMap,
      }
    })
    get().saveCurrentTabs()

    // 关闭 Tab 后，若回到主 Tab 则重新激活主管道
    const { currentSessionId, tabs, activeTabId } = get()
    if (currentSessionId) {
      const mainTab = tabs.find((t) => t.agentLevel === 1)
      if (mainTab && activeTabId === mainTab.id) {
        usePipelineMessageStore.getState().activatePipeline(currentSessionId)
      }
    }
  },

  /**
   * 切换�?Tab（增强版，自动清除未读，激活对应管道）
   *
   * BUG-FIX-fix_20260510_tab_blank_on_switch:
   * 问题根因: �?set({activeTabId}) 触发 MessageList 销毁重建，
   *          此时 pipelineMessages 还指向旧管道或为空，导致短暂空白�?
   * 修复方案: 先激活管道并同步消息，再切换 activeTabId�?
   *          确保 MessageList 重建时消息数据已就绪�?
   */
  switchToTab: (tabId) => {
    const { tabs, tabMessages, activeTabId: prevActiveTabId } = get()
    const tab = tabs.find((t) => t.id === tabId)

    if (!tab) {
      console.warn(`[AgentTabStore] Tab not found: ${tabId}`)
      return
    }

    if (prevActiveTabId === tabId) return

    const pipelineStore = usePipelineMessageStore.getState()
    if (tab.agentLevel === 1) {
      const { currentSessionId } = get()
      if (currentSessionId) {
        pipelineStore.activatePipeline(currentSessionId)
      }
    } else {
      const effectivePipelineId = tab.pipelineRunId || tabId
      pipelineStore.activatePipeline(effectivePipelineId)
      // BUG-FIX-fix_20260512_msg_order:
      // 问题根因: 之前用 initFromAPI 完全替换消息列表，会用过期缓存覆盖实时流式数据。
      //          当用户在流式输出期间切换 Tab 再切回来，实时消息会被旧缓存覆盖。
      // 修复方案: 只在管道消息为空时才用缓存初始化，否则保留已有消息。
      //          loadTabMessages 在后面会异步刷新 API 数据。
      // 影响范围: Tab 切换时的消息显示
      // 修复日期: 2026-05-12
      const existingPipelineMessages = pipelineStore.getMessages(effectivePipelineId)
      if (!existingPipelineMessages || existingPipelineMessages.length === 0) {
        const cached = tabMessages[tabId]
        if (cached && cached.length > 0) {
          pipelineStore.initFromAPI(effectivePipelineId, cached)
        }
      }
    }

    set({ activeTabId: tabId })
    get().clearTabUnread(tabId)

    if (tab.agentLevel !== 1) {
      get().loadTabMessages(tabId)
    }
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
   * 更新 Tab 状态（别名�?
   */
  updateTab: (tabId, updates) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, ...updates } : t)),
    }))
  },

  /**
   * 打开�?Agent Tab（统一接口�?
   *
   * BUG-FIX-fix_20260510_tab_blank_on_open:
   * 问题根因: set() 中同时设�?activeTabId，触�?MessageList 重建�?
   *          但此时管道消息尚未同步，导致短暂空白�?
   * 修复方案: 分两阶段操作 �?先创�?Tab（不激活），再同步管道消息�?
   *          最后设�?activeTabId，确�?MessageList 重建时数据已就绪�?
   */
  openSubAgentTab: (params) => {
    const {
      agentId,
      agentName,
      parentRecordId,
      agentLevel = 2,
      taskId,
      status = 'running',
      setActive = false,
      pipelineId,
    } = params

    const tabId = `sub-${parentRecordId}`

    set((state) => {
      const path = ['主Agent', agentName]
      const existingTab = state.tabs.find((t) => t.id === tabId)

      if (existingTab) {
        return {
          tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status } : t)),
        }
      }

      const newTab: AgentTab = {
        id: tabId,
        agentId,
        agentName,
        agentLevel,
        taskId,
        parentRecordId,
        pipelineRunId: pipelineId,
        path,
        status,
        hasUnread: false,
        canClose: agentLevel !== 1,
        messages: [],
      }

      return {
        tabs: [...state.tabs, newTab],
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

    if (pipelineId) {
      get().registerPipelineTab(pipelineId, tabId)
    }

    if (setActive) {
      const effectivePipelineId = pipelineId || tabId
      const pipelineStore = usePipelineMessageStore.getState()
      pipelineStore.activatePipeline(effectivePipelineId)

      const cached = get().tabMessages[tabId]
      if (cached && cached.length > 0) {
        pipelineStore.initFromAPI(effectivePipelineId, cached)
      }

      set({ activeTabId: tabId })
    }
  },

  /**
   * 注册 pipeline_id �?tabId 映射（注册后自动持久化）
   */
  registerPipelineTab: (pipelineId, tabId) => {
    set((state) => ({
      pipelineTabMap: {
        ...state.pipelineTabMap,
        [pipelineId]: tabId,
      },
    }))
    get().saveCurrentTabs()
  },

  /**
   * 根据 pipeline_id 查找对应�?tabId
   */
  getTabIdByPipeline: (pipelineId) => {
    return get().pipelineTabMap[pipelineId]
  },

  /**
   * 从后�?API 加载�?Tab 消息（持久化恢复�?
   *
   * 逻辑�?
   * 1. 如果已有消息（tabMessages[tabId] 非空），跳过加载
   * 2. 如果正在加载中（tabMessagesLoading[tabId]），跳过避免并发
   * 3. 通过 getMessages API 传入 pipelineRunId 参数获取子管道消�?
   * 4. 加载成功后写�?tabMessages[tabId] 并同步保存到 localStorage
   *
   * @param tabId - 标签�?ID
   * @param pipelineRunId - 可选，管道运行实例 ID（用于加载子管道消息�?
   */
  loadTabMessages: async (tabId, pipelineRunId) => {
    const state = get()

    // BUG-FIX-fix_20260512_msg_disappear:
    // 问题根因: 之前只要有缓存就跳过 API 加载，但 localStorage 恢复的缓存可能不完整
    //          （只保存了最后50条、或流式消息尚未同步），导致刷新后消息永远不完整。
    // 修复方案: 移除缓存跳过逻辑，始终尝试从 API 加载最新数据。
    //          API 加载成功后会通过 initFromAPI 覆盖缓存数据，确保数据最新。
    //          如果 API 返回 404（子 Agent 尚未执行），则保留缓存数据。
    // 影响范围: 刷新页面后的子 Tab 消息加载
    // 修复日期: 2026-05-12

    // 防止并发加载
    if (state.tabMessagesLoading[tabId]) return

    const tab = state.tabs.find((t) => t.id === tabId)
    if (!tab || !state.currentSessionId) return

    // 子 Tab 的 pipelineRunId 就是管道 ID，直接用它作为消息定位键
    const effectivePipelineId = pipelineRunId || tab.pipelineRunId
    if (!effectivePipelineId) return

    // 标记加载中
    set((s) => ({
      tabMessagesLoading: {
        ...s.tabMessagesLoading,
        [tabId]: true,
      },
    }))

    try {
      // BUG-FIX-fix_20260512_unnecessary_filter:
      // 问题根因: 之前用 pipelineRunId/parentId 过滤参数调 API，
      //          但后端 get_thread_messages 不支持 pipeline_run_id 参数（被忽略），
      //          parentId 过滤则是画蛇添足——管道 ID 已足够定位消息流。
      // 修复方案: 直接用 pipelineId 作为 sessionId 调 getMessages，
      //          与 pipelineMessageStore.fetchMessages 保持一致，不需要任何额外过滤参数。
      // 影响范围: 子 Tab 消息加载
      // 修复日期: 2026-05-12
      const messages = await getMessages(effectivePipelineId)
      const loadedMessages = messages.messages || []

      set((s) => ({
        tabMessages: {
          ...s.tabMessages,
          [tabId]: loadedMessages,
        },
        tabMessagesLoading: {
          ...s.tabMessagesLoading,
          [tabId]: false,
        },
      }))

      // 同步加载的消息到 pipelineMessageStore
      // BUG-FIX-fix_20260509_tab_blank:
      // 确保消息设置到与 activePipelineId 一致的 key 上，
      // 并在加载完成后重新激活管道以确保 ChatContainer selector 触发更新
      if (loadedMessages.length > 0) {
        const targetPipelineId = effectivePipelineId || tabId
        const pipelineStore = usePipelineMessageStore.getState()
        pipelineStore.initFromAPI(targetPipelineId, loadedMessages)
        // 如果当前 tab 仍然是活跃的，确保管道处于激活状�?
        if (get().activeTabId === tabId) {
          pipelineStore.activatePipeline(targetPipelineId)
        }
      }

      // 同步保存�?localStorage 缓存
      get().saveCurrentTabs()
    } catch (error: any) {
      // BUG-FIX-fix_20260511_load_tab_404:
      // 问题根因: �?Tab �?pipelineRunId �?parentRecordId 在后端可能尚不存�?
      //          （子 Agent 还未开始执行），API 返回 404 是正常场景，
      //          但之前统一�?console.error 输出，造成大量误导性错误日�?
      // 修复方案: 区分 404（资源不存在，静默处理）和其他错误（保留 error 日志�?
      const is404 =
        error?.response?.status === 404 ||
        error?.message?.includes('404') ||
        error?.code === '404'

      if (is404) {
        // 404 是正常场景：�?Agent 可能尚未开始执行，对应消息还不存在
        console.debug(
          `[AgentTabStore.loadTabMessages] �?Tab 消息暂不可用 (404) | tabId: ${tabId}`,
        )
      } else {
        console.error('[AgentTabStore.loadTabMessages] 加载�?Tab 消息失败:', error)
      }

      // 加载失败时静默处理，清除加载中标�?
      set((s) => ({
        tabMessagesLoading: {
          ...s.tabMessagesLoading,
          [tabId]: false,
        },
      }))
    }
  },
}))
