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
import { useSessionStore } from '@/stores/sessionStore'
import type { AgentTab } from '@/types/task'

/**
 * 获取主管道 ID
 *
 * BUG-FIX-fix_20260513_main_tab_pipeline:
 * 问题根因: 主管道消息以 session.activePipelineId 为 key 存储在 pipelineMessageStore 中，
 *          但 switchToTab/closeTab 用 currentSessionId 激活管道，两者不一致导致消息不显示。
 * 修复方案: 统一通过此函数获取主管道 ID，确保管道激活与消息存储使用同一 key。
 */
function getMainPipelineId(sessionId: string): string | null {
  const sessions = useSessionStore.getState().sessions
  const session = sessions.find((s) => s.id === sessionId)
  return session?.activePipelineId || session?.pipelineIds?.[0] || null
}

/** localStorage 存储键前缀 */
const STORAGE_KEY_PREFIX = 'agent-tabs-'
/** 每个 Tab 缓存到 localStorage 的最大消息条数 */
const MAX_CACHED_MESSAGES_PER_TAB = 50
/** 降级策略：消息截取数量的递减阶梯 */
const MESSAGE_LIMIT_STEPS = [MAX_CACHED_MESSAGES_PER_TAB, 10, 0]

/**
 * 获取会话对应的存储键
 */
function getStorageKey(sessionId: string): string {
  return `${STORAGE_KEY_PREFIX}${sessionId}`
}

/**
 * 清理其他会话的过期 localStorage 数据，释放空间
 * 保留当前会话，按 savedAt 时间排序，优先清理最旧的数据
 */
function cleanupExpiredSessionData(currentSessionId: string): void {
  const allKeys: { key: string; savedAt: number }[] = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key && key.startsWith(STORAGE_KEY_PREFIX) && key !== getStorageKey(currentSessionId)) {
      try {
        const raw = localStorage.getItem(key)
        if (raw) {
          const data = JSON.parse(raw)
          allKeys.push({ key, savedAt: data.savedAt || 0 })
        }
      } catch {
        allKeys.push({ key, savedAt: 0 })
      }
    }
  }
  allKeys.sort((a, b) => a.savedAt - b.savedAt)
  for (const { key } of allKeys) {
    localStorage.removeItem(key)
  }
}

/**
 * 尝试用指定消息数量限制保存数据到 localStorage
 */
function trySaveWithLimit(
  sessionId: string,
  tabs: AgentTab[],
  activeTabId: string | null,
  tabMessages: Record<string, any[]>,
  pipelineTabMap: Record<string, string>,
  messageLimit: number,
): boolean {
  const cachedMessages: Record<string, any[]> = {}
  if (messageLimit > 0) {
    for (const tabId of Object.keys(tabMessages)) {
      const msgs = tabMessages[tabId] || []
      if (msgs.length > 0) {
        cachedMessages[tabId] = msgs.slice(-messageLimit)
      }
    }
  }
  const data = { tabs, activeTabId, tabMessages: cachedMessages, pipelineTabMap, savedAt: Date.now() }
  localStorage.setItem(getStorageKey(sessionId), JSON.stringify(data))
  return true
}

/**
 * 保存标签状态到 localStorage（包含最近 N 条消息缓存和 pipeline 映射）
 *
 * 采用渐进式降级策略应对 QuotaExceededError：
 * 1. 按 50 → 10 → 0 递减消息数量重试
 * 2. 仍失败则清理其他会话的旧数据后重试
 */
function saveTabsToStorage(
  sessionId: string,
  tabs: AgentTab[],
  activeTabId: string | null,
  tabMessages: Record<string, any[]>,
  pipelineTabMap: Record<string, string>,
): void {
  for (const limit of MESSAGE_LIMIT_STEPS) {
    try {
      trySaveWithLimit(sessionId, tabs, activeTabId, tabMessages, pipelineTabMap, limit)
      return
    } catch (e) {
      if (!(e instanceof DOMException && e.name === 'QuotaExceededError')) {
        console.warn('[AgentTabStore] 保存标签状态失败', e)
        return
      }
    }
  }

  try {
    cleanupExpiredSessionData(sessionId)
    trySaveWithLimit(sessionId, tabs, activeTabId, tabMessages, pipelineTabMap, 0)
    return
  } catch {
    // 清理后仍然失败，放弃本次保存
  }

  console.warn('[AgentTabStore] 保存标签状态失败：localStorage 配额不足，已清理旧数据仍无法写入')
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
      set({
        currentSessionId: sessionId,
        tabs: saved.tabs,
        activeTabId: saved.activeTabId || null,
        tabMessages: {},
        tabMessagesLoading: {},
        unreadCounts: {},
        pipelineTabMap: saved.pipelineTabMap || {},
      })

      const pipelineStore = usePipelineMessageStore.getState()
      saved.tabs.forEach((tab) => {
        if (tab.agentLevel !== 1 && tab.pipelineRunId) {
          if (!pipelineStore.pipelines[tab.pipelineRunId]) {
            pipelineStore.registerPipeline({
              pipelineId: tab.pipelineRunId,
              sessionId,
              level: tab.agentLevel as 1 | 2 | 3,
              tabId: tab.id,
              agentName: tab.agentName,
              status: 'running',
              parentId: sessionId,
              unreadCount: 0,
            })
          }
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
   *
   * BUG-FIX-fix_20260523_removetab_pipeline_map:
   * 问题根因: removeTab 不清理 pipelineTabMap 中指向被移除 tab 的映射，
   *          而 closeTab 会清理，导致 pipelineTabMap 可能留下指向不存在 tabId 的脏映射。
   * 修复方案: 在 removeTab 中添加 pipelineTabMap 的清理逻辑，与 closeTab 保持一致。
   * 影响范围: Tab 移除后的 pipeline 映射一致性
   * 修复日期: 2026-05-23
   */
  removeTab: (tabId) => {
    set((state) => {
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
        // BUG-FIX-fix_20260513_main_tab_pipeline:
        // 问题根因: 同 switchToTab，用 currentSessionId 而非 actual pipelineId
        // 修复方案: 通过 getMainPipelineId() 获取实际的管道 ID
        // 影响范围: 关闭子 Tab 后回退到主 Tab 时的消息显示
        // 修复日期: 2026-05-13
        const mainPipelineId = getMainPipelineId(currentSessionId)
        if (mainPipelineId) {
          pipelineStore.activatePipeline(mainPipelineId)
          if (!pipelineStore.messagesByPipeline[mainPipelineId]?.length) {
            pipelineStore.fetchMessages(mainPipelineId, { threadId: currentSessionId })
          }
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
        // BUG-FIX-fix_20260513_main_tab_pipeline:
        // 问题根因: 之前用 currentSessionId 激活管道，但主管道消息以 session.activePipelineId
        //          为 key 存储在 pipelineMessageStore 中，两者不一致导致 pipelineMessages 读到空数组。
        // 修复方案: 通过 getMainPipelineId() 获取实际的管道 ID 来激活。
        // 影响范围: 从子 Tab 切换回主 Tab 时的消息显示
        // 修复日期: 2026-05-13
        const mainPipelineId = getMainPipelineId(currentSessionId)
        if (mainPipelineId) {
          pipelineStore.activatePipeline(mainPipelineId)
        }
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

    // BUG-FIX-fix_20260513_tab_not_restored:
    // 问题根因: switchToTab 切换标签后未调用 saveCurrentTabs()，
    //          导致 localStorage 中保存的仍是旧的 activeTabId，
    //          刷新页面后恢复到错误的标签（如上次选中的子标签而非当前主标签）。
    // 修复方案: 切换完成后调用 saveCurrentTabs() 持久化当前标签状态。
    // 影响范围: 标签切换后刷新页面的标签恢复
    // 修复日期: 2026-05-13
    get().saveCurrentTabs()
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
      const pipelineStore = usePipelineMessageStore.getState()
      if (!pipelineStore.pipelines[pipelineId]) {
        pipelineStore.registerPipeline({
          pipelineId,
          sessionId: get().currentSessionId || '',
          level: agentLevel,
          tabId,
          agentName,
          status: 'running',
          parentId: get().currentSessionId || '',
          unreadCount: 0,
        })
      }
    }

    if (setActive) {
      const effectivePipelineId = pipelineId || tabId
      const pipelineStore = usePipelineMessageStore.getState()
      pipelineStore.activatePipeline(effectivePipelineId)
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
      const pipelineStore = usePipelineMessageStore.getState()
      if (!pipelineStore.pipelines[effectivePipelineId]) {
        pipelineStore.registerPipeline({
          pipelineId: effectivePipelineId,
          sessionId: state.currentSessionId,
          level: (tab.agentLevel as 1 | 2 | 3) || 2,
          tabId,
          agentName: tab.agentName,
          status: 'running',
          parentId: state.currentSessionId,
          unreadCount: 0,
        })
      }

      // BUG-FIX-fix_20260515_streaming_interrupt:
      // 问题根因: 切换标签/会话时 loadTabMessages 无条件调用 fetchMessages -> initFromAPI，
      //          后端 API 返回的数据（可能是已完成状态）会覆盖正在流式传输的消息，
      //          导致流式输出中断、内容丢失、状态错误。
      // 修复方案: 管道正在流式传输时跳过 API 请求，保留本地的流式数据。
      //          流式数据由 WebSocket 全局事件处理器持续更新，不受组件挂载/卸载影响。
      //          切换回来时 activatePipeline 即可恢复显示，无需重新获取。
      // 影响范围: 标签切换、会话切换时的流式输出连续性
      // 修复日期: 2026-05-15
      if (!pipelineStore.isStreaming(effectivePipelineId)) {
        await pipelineStore.fetchMessages(effectivePipelineId, { threadId: state.currentSessionId })
      }

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
