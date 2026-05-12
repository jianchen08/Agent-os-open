/**
 * Agent Tab 状态管理 Store
 *
 * 管理 Agent Tab 的状态，支持：
 * - 添加/移除 Agent Tab
 * - 切换活跃 Tab
 * - 更新 Tab 状态
 * - 每个 Tab 独立的消息列表
 * - localStorage 持久化（按会话存储标签状态）
 *
 * 消息读写统一走 pipelineMessageStore，tabMessages 仅作为空壳保留（避免破坏类型）。
 */

import { create } from 'zustand'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { AgentTab } from '@/types/task'

/** localStorage 存储键前缀 */
const STORAGE_KEY_PREFIX = 'agent-tabs-'
/** 每个 Tab 缓存到 localStorage 的最大消息条数 */
const MAX_CACHED_MESSAGES_PER_TAB = 50

/**
 * 获取会话对应的存储键
 */
function getStorageKey(sessionId: string): string {
  return `${STORAGE_KEY_PREFIX}${sessionId}`
}

/**
 * 保存标签状态到 localStorage（包含最近 N 条消息缓存和 pipeline 映射）
 */
function saveTabsToStorage(
  sessionId: string,
  tabs: AgentTab[],
  activeTabId: string | null,
  tabMessages: Record<string, any[]>,
  pipelineTabMap: Record<string, string>,
): void {
  try {
    // 对每个 Tab 的消息截取最近 N 条，避免 localStorage 超限
    const cachedMessages: Record<string, any[]> = {}
    for (const tabId of Object.keys(tabMessages)) {
      const msgs = tabMessages[tabId] || []
      cachedMessages[tabId] = msgs.slice(-MAX_CACHED_MESSAGES_PER_TAB)
    }
    const data = { tabs, activeTabId, tabMessages: cachedMessages, pipelineTabMap, savedAt: Date.now() }
    localStorage.setItem(getStorageKey(sessionId), JSON.stringify(data))
  } catch (e) {
    console.warn('[AgentTabStore] 保存标签状态失败', e)
  }
}

/**
 * 从 localStorage 加载标签状态（含缓存消息和 pipeline 映射）
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
    console.warn('[AgentTabStore] 加载标签状态失败', e)
    return null
  }
}

/**
 * Agent Tab 状态接口
 */
interface AgentTabState {
  /** Agent Tab 列表 */
  tabs: AgentTab[]
  /** 当前活跃 Tab ID */
  activeTabId: string | null
  /** 每个 Tab 的消息映射(tabId -> messages) —— 空壳，实际数据在 pipelineMessageStore */
  tabMessages: Record<string, any[]>
  /** 每个 Tab 的消息加载状态（防止并发重复加载） */
  tabMessagesLoading: Record<string, boolean>
  /** 每个 Tab 的未读消息计数(tabId -> count) */
  unreadCounts: Record<string, number>
  /** 当前会话 ID（用于持久化标识） */
  currentSessionId: string | null
  /** pipeline_id → tabId 映射（用于流式消息路由到对应 Tab） */
  pipelineTabMap: Record<string, string>

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
  /** 切换子 Tab（增强版，自动清除未读） */
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
    /** pipeline_id，用于后续流式消息路由到该子 Tab */
    pipelineId?: string
  }) => void
  /** 注册 pipeline_id → tabId 映射 */
  registerPipelineTab: (pipelineId: string, tabId: string) => void
  /** 根据 pipeline_id 查找对应的 tabId */
  getTabIdByPipeline: (pipelineId: string) => string | undefined
  /** 从后端 API 加载子 Tab 消息（持久化恢复） */
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
   * 初始化/切换会话标签（从 localStorage 恢复）
   * 不再加载 tabMessages 缓存，子 Tab 消息通过 pipelineMessageStore 异步加载
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
        tabMessagesLoading: {},
        unreadCounts: {},
        pipelineTabMap: saved.pipelineTabMap || {},
      })

      // 后台异步：对有 pipelineRunId 的子 Tab，从 API 加载消息到 pipelineMessageStore
      // 使用 loadTabMessages 而非直接调 fetchMessages，因为 loadTabMessages 有 404 静默处理
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
   * 保存当前标签状态到 localStorage
   * 从 pipelineMessageStore 读取消息构建缓存，而非 tabMessages
   */
  saveCurrentTabs: () => {
    const { currentSessionId, tabs, activeTabId, pipelineTabMap } = get()
    if (!currentSessionId) return

    const pipelineStore = usePipelineMessageStore.getState()
    const cachedMessages: Record<string, any[]> = {}

    for (const tab of tabs) {
      if (tab.agentLevel === 1) {
        const msgs = pipelineStore.getMessages(currentSessionId)
        if (msgs.length > 0) {
          cachedMessages[tab.id] = msgs.slice(-MAX_CACHED_MESSAGES_PER_TAB)
        }
      } else if (tab.pipelineRunId) {
        const msgs = pipelineStore.getMessages(tab.pipelineRunId)
        if (msgs.length > 0) {
          cachedMessages[tab.id] = msgs.slice(-MAX_CACHED_MESSAGES_PER_TAB)
        }
      }
    }

    saveTabsToStorage(currentSessionId, tabs, activeTabId, cachedMessages, pipelineTabMap)
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
      const newUnreadCounts = { ...state.unreadCounts }

      delete newUnreadCounts[tabId]

      let newActiveTabId = state.activeTabId
      if (state.activeTabId === tabId) {
        const mainTab = newTabs.find((t) => t.agentLevel === 1)
        newActiveTabId = mainTab?.id || newTabs[0]?.id || null
      }

      return {
        tabs: newTabs,
        activeTabId: newActiveTabId,
        unreadCounts: newUnreadCounts,
      }
    })
    get().saveCurrentTabs()
  },

  /**
   * 设置活跃 Tab（子 Tab 时自动从后端加载消息）
   */
  setActiveTab: (tabId) => {
    set({
      activeTabId: tabId,
    })

    get().clearTabUnread(tabId)
    get().saveCurrentTabs()

    // 子 Tab 切换时触发消息持久化加载
    const tab = get().tabs.find((t) => t.id === tabId)
    if (tab && tab.parentRecordId && tab.agentLevel !== 1) {
      get().loadTabMessages(tabId)
    }
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
   * 消息写入 pipelineMessageStore 而非 tabMessages
   */
  addMessageToTab: (tabId, message) => {
    const { tabs, pipelineTabMap, activeTabId } = get()
    const tab = tabs.find((t) => t.id === tabId)

    let pipelineId: string | null = null
    if (tab?.pipelineRunId) {
      pipelineId = tab.pipelineRunId
    } else if (tab?.agentLevel === 1) {
      pipelineId = get().currentSessionId
    } else {
      // 通过 pipelineTabMap 反向查找 pipelineId
      for (const [pid, tid] of Object.entries(pipelineTabMap)) {
        if (tid === tabId) {
          pipelineId = pid
          break
        }
      }
    }

    if (pipelineId) {
      usePipelineMessageStore.getState().addMessage(pipelineId, message)
    }

    // 非活跃 Tab 更新未读计数
    if (activeTabId !== tabId) {
      set((state) => ({
        unreadCounts: {
          ...state.unreadCounts,
          [tabId]: (state.unreadCounts[tabId] || 0) + 1,
        },
        tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, hasUnread: true } : t)),
      }))
    }
  },

  /**
   * 获取当前活跃 Tab 的消息
   * 从 pipelineMessageStore 读取
   */
  getActiveTabMessages: () => {
    const { activeTabId, tabs, currentSessionId } = get()
    if (!activeTabId || !currentSessionId) return []
    const tab = tabs.find((t) => t.id === activeTabId)
    if (!tab) return []
    const pipelineId = tab.agentLevel === 1 ? currentSessionId : tab.pipelineRunId
    if (!pipelineId) return []
    return usePipelineMessageStore.getState().getMessages(pipelineId)
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
      tabMessagesLoading: {},
      unreadCounts: {},
      pipelineTabMap: {},
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
        unreadCounts: {
          ...state.unreadCounts,
          [tabData.id]: 0,
        },
      }
    })
  },

  /**
   * 关闭 Tab（增强版，支持主 Tab 保护，同时清理 pipeline 映射）
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
      const newUnreadCounts = { ...state.unreadCounts }

      delete newUnreadCounts[tabId]

      // 清理指向该 Tab 的 pipeline 映射
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
        unreadCounts: newUnreadCounts,
        pipelineTabMap: newPipelineTabMap,
      }
    })
    get().saveCurrentTabs()

    const { currentSessionId, tabs, activeTabId } = get()
    if (currentSessionId) {
      const mainTab = tabs.find((t) => t.agentLevel === 1)
      if (mainTab && activeTabId === mainTab.id) {
        const pipelineStore = usePipelineMessageStore.getState()
        pipelineStore.activatePipeline(currentSessionId)
        if (!pipelineStore.messagesByPipeline[currentSessionId]?.length) {
          pipelineStore.fetchMessages(currentSessionId, { threadId: currentSessionId })
        }
      }
    }
  },

  /**
   * 切换子 Tab（增强版，自动清除未读，激活对应管道）
   *
   * BUG-FIX-fix_20260510_tab_blank_on_switch:
   * 问题根因: 单 set({activeTabId}) 触发 MessageList 销毁重建，
   *          此时 pipelineMessages 还指向旧管道或为空，导致短暂空白。
   * 修复方案: 先激活管道并同步消息，再切换 activeTabId，
   *          确保 MessageList 重建时消息数据已就绪。
   */
  switchToTab: (tabId) => {
    const { tabs, activeTabId: prevActiveTabId } = get()
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
      // 问题根因: 缓存预填 initFromAPI 与异步 loadTabMessages 的 initFromAPI 之间存在竞争，
      //          若两次 initFromAPI 之间有 WebSocket 事件到达，事件写入的数据会被第二次完全覆盖。
      // 修复方案: 移除缓存预填，仅保留 activatePipeline 确保管道切换立即生效。
      //          loadTabMessages 在后面会异步加载 API 数据并写入 pipelineMessageStore。
      //          API 数据到达前，ChatContainer 已有 fallback 到 messages prop 的逻辑。
      // 影响范围: Tab 切换时的消息显示
      // 修复日期: 2026-05-12
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
   * 从 pipelineMessageStore 读取消息并合并
   */
  mergeToMainTab: (subTabId) => {
    set((state) => {
      const subTab = state.tabs.find((t) => t.id === subTabId)
      const mainTab = state.tabs.find((t) => t.agentLevel === 1)

      if (!subTab || !mainTab || !state.currentSessionId) {
        console.warn('[AgentTabStore] Cannot merge: subTab or mainTab not found')
        return state
      }

      const pipelineStore = usePipelineMessageStore.getState()
      const mainPipelineId = state.currentSessionId

      // 从 pipelineMessageStore 读取子 Tab 消息并合并到主管道
      if (subTab.pipelineRunId) {
        const subMsgs = pipelineStore.getMessages(subTab.pipelineRunId)
        const mainMsgs = pipelineStore.getMessages(mainPipelineId)
        const merged = [
          ...mainMsgs,
          ...subMsgs.map((msg) => ({
            ...msg,
            metadata: {
              ...msg.metadata,
              mergedFrom: subTabId,
              mergedAt: new Date().toISOString(),
            },
          })),
        ]
        pipelineStore.initFromAPI(mainPipelineId, merged)
      }

      // 清理 pipelineTabMap 中指向已合并子 Tab 的映射
      const newPipelineTabMap = { ...state.pipelineTabMap }
      for (const [pid, tid] of Object.entries(newPipelineTabMap)) {
        if (tid === subTabId) delete newPipelineTabMap[pid]
      }

      const newTabs = state.tabs.filter((t) => t.id !== subTabId)
      let newActiveTabId = state.activeTabId
      if (state.activeTabId === subTabId) {
        newActiveTabId = mainTab.id
      }

      return {
        tabs: newTabs,
        activeTabId: newActiveTabId,
        pipelineTabMap: newPipelineTabMap,
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
   *
   * BUG-FIX-fix_20260510_tab_blank_on_open:
   * 问题根因: set() 中同时设置 activeTabId，触发 MessageList 重建，
   *          但此时管道消息尚未同步，导致短暂空白。
   * 修复方案: 分两阶段操作 — 先创建 Tab（不激活），再同步管道消息，
   *          最后设置 activeTabId，确保 MessageList 重建时数据已就绪。
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
      // BUG-FIX-fix_20260512_msg_order: 移除缓存预填，避免与后续 loadTabMessages 竞争覆盖 WebSocket 数据
      set({ activeTabId: tabId })
    }
  },

  /**
   * 注册 pipeline_id → tabId 映射（注册后自动持久化）
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
   * 根据 pipeline_id 查找对应的 tabId
   */
  getTabIdByPipeline: (pipelineId) => {
    return get().pipelineTabMap[pipelineId]
  },

  /**
   * 从后端 API 加载子 Tab 消息（持久化恢复）
   *
   * 历史修复记录（逻辑已被 pipelineStore.fetchMessages 替代）：
   * - BUG-FIX-fix_20260512_msg_disappear: 移除缓存跳过，始终从 API 加载最新数据
   * - BUG-FIX-fix_20260512_unnecessary_filter: 直接用 pipelineId 调 API，无需额外过滤参数
   * - BUG-FIX-fix_20260509_tab_blank: 加载后激活管道确保 ChatContainer 更新
   * - BUG-FIX-fix_20260511_load_tab_404: 404 静默处理
   *
   * @param tabId - 标签页 ID
   * @param pipelineRunId - 可选，管道运行实例 ID
   */
  loadTabMessages: async (tabId, pipelineRunId) => {
    const state = get()

    // 防止并发加载
    if (state.tabMessagesLoading[tabId]) return

    const tab = state.tabs.find((t) => t.id === tabId)
    if (!tab || !state.currentSessionId) return

    const effectivePipelineId = pipelineRunId || tab.pipelineRunId
    if (!effectivePipelineId) return

    set((s) => ({
      tabMessagesLoading: { ...s.tabMessagesLoading, [tabId]: true },
    }))

    try {
      // BUG-FIX-fix_20260512_fetch_threadid: 传入 threadId（currentSessionId），
      // 确保 fetchMessages 用正确的 threadId 查询后端 API，而非用 pipelineId 当 threadId
      const pipelineStore = usePipelineMessageStore.getState()
      await pipelineStore.fetchMessages(effectivePipelineId, { threadId: state.currentSessionId })

      if (get().activeTabId === tabId) {
        pipelineStore.activatePipeline(effectivePipelineId)
      }

      get().saveCurrentTabs()
    } catch (error: any) {
      const is404 =
        error?.response?.status === 404 ||
        error?.message?.includes('404') ||
        error?.code === '404'

      if (is404) {
        console.debug(
          `[AgentTabStore.loadTabMessages] 子Tab消息暂不可用 (404) | tabId: ${tabId}`,
        )
      } else {
        console.error('[AgentTabStore.loadTabMessages] 加载子Tab消息失败:', error)
      }
    } finally {
      set((s) => ({
        tabMessagesLoading: { ...s.tabMessagesLoading, [tabId]: false },
      }))
    }
  },
}))
