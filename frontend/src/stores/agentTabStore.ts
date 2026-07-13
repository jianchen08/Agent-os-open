/** Agent Tab 状态管理 Store 管理 Agent Tab 的状态，支持： */

import { create } from 'zustand'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import type { AgentTab } from '@/types/task'

/** 获取主管道 ID */
function getMainPipelineId(sessionId: string): string | null {
  const sessions = useSessionStore.getState().sessions
  const session = sessions.find((s) => s.id === sessionId)
  const mainPid = session?.pipelineIds?.[0]
  if (!mainPid) {
    console.warn('[getMainPipelineId] 主管道缺失: sessionId=%s pipelineIds=%o', sessionId, session?.pipelineIds)
  }
  return mainPid ?? null
}

/** 获取主管道对应的主 Agent ID 后端创建会话时默认回填 agent_id="lingxi"（routes_threads.py）。 */
function getMainAgentId(sessionId: string): string {
  const sessions = useSessionStore.getState().sessions
  const session = sessions.find((s) => s.id === sessionId)
  return session?.agentId || 'lingxi'
}

/** localStorage 存储键前缀 */
const STORAGE_KEY_PREFIX = 'agent-tabs-'

/** 消息缓存旧 localStorage key（已迁移到 IndexedDB，残留于此则清理释放空间） */
const LEGACY_PIPELINE_MESSAGES_KEY = 'pipeline-messages'

/** 获取会话对应的存储键 */
function getStorageKey(sessionId: string): string {
  return `${STORAGE_KEY_PREFIX}${sessionId}`
}

/**
 * 配额不足时清理 localStorage 释放空间。
 *
 * 两步：
 * 1. 清理其他会话的过期 agent-tabs-* 数据（保留当前会话，按 savedAt 优先清最旧）；
 * 2. 删除旧版残留的 pipeline-messages key（消息缓存已迁 IndexedDB，
 *    localStorage 里这份是历史遗留，删掉可释放可观空间）。
 *
 * 注意：迁移后正常情况 pipeline-messages 已由 onRehydrateStorage 一次性清理，
 * 此处作为兜底（如 IndexedDB 降级、rehydrate 未触发等边缘场景）。
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

  // 清理旧版消息缓存残留（消息存储已迁至 IndexedDB）。
  try {
    if (localStorage.getItem(LEGACY_PIPELINE_MESSAGES_KEY) !== null) {
      localStorage.removeItem(LEGACY_PIPELINE_MESSAGES_KEY)
    }
  } catch {
    // localStorage 不可用时忽略
  }
}

/** 尝试保存标签状态到 localStorage */
function trySaveTabs(
  sessionId: string,
  tabs: AgentTab[],
  activeTabId: string | null,
  pipelineTabMap: Record<string, string>,
): boolean {
  const data = { tabs, activeTabId, pipelineTabMap, savedAt: Date.now() }
  localStorage.setItem(getStorageKey(sessionId), JSON.stringify(data))
  return true
}

/** 保存标签状态到 localStorage（标签 + pipeline 映射）。 降级策略：写入遇 QuotaExceededError 时清理其他会话旧数据后重试一次。 */
function saveTabsToStorage(
  sessionId: string,
  tabs: AgentTab[],
  activeTabId: string | null,
  pipelineTabMap: Record<string, string>,
): void {
  try {
    trySaveTabs(sessionId, tabs, activeTabId, pipelineTabMap)
    return
  } catch (e) {
    if (!(e instanceof DOMException && e.name === 'QuotaExceededError')) {
      console.warn('[AgentTabStore] 保存标签状态失败', e)
      return
    }
  }

  try {
    cleanupExpiredSessionData(sessionId)
    trySaveTabs(sessionId, tabs, activeTabId, pipelineTabMap)
    return
  } catch {
    // 清理后仍然失败，放弃本次保存
  }

  console.warn('[AgentTabStore] 保存标签状态失败：localStorage 配额不足，已清理旧数据仍无法写入')
}

/** 从 localStorage 加载标签状态（标签 + pipeline 映射）。 旧版本写入的 tabMessages 字段会被自然忽略（不再读取）。 */
function loadTabsFromStorage(
  sessionId: string,
): { tabs: AgentTab[]; activeTabId: string | null; pipelineTabMap: Record<string, string> } | null {
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
      pipelineTabMap: data.pipelineTabMap || {},
    }
  } catch (e) {
    console.warn('[AgentTabStore] 加载标签状态失败', e)
    return null
  }
}

/** Agent Tab 状态接口 */
interface AgentTabState {
  /** Agent Tab 列表 */
  tabs: AgentTab[]
  /** 当前活跃 Tab ID */
  activeTabId: string | null
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

/** Agent Tab Store */
export const useAgentTabStore = create<AgentTabState>((set, get) => ({
  tabs: [],
  activeTabId: null,
  tabMessagesLoading: {},
  unreadCounts: {},
  currentSessionId: null,
  pipelineTabMap: {},

  /** 初始化/切换会话标签（从 localStorage 恢复） 核心规则：activeTab.pipelineRunId 是加载管道的唯一依据。 */
  initSessionTabs: (sessionId) => {
    const saved = loadTabsFromStorage(sessionId)
    const mainPipelineId = getMainPipelineId(sessionId)
    const mainAgentId = getMainAgentId(sessionId)

    let tabs: AgentTab[]
    let activeTabId: string | null

    if (saved && saved.tabs.length > 0) {
      // 主管道 pipelineRunId 始终用 session 提供的最新 ID（与 fetchMessages 一致）
      // agentId 同步为 session 的主 agent（消除旧缓存写死的空串）
      // 子 Tab 缺 pipelineRunId 时保持 undefined（不污染）
      tabs = saved.tabs.map((tab) => {
        if (tab.agentLevel === 1) {
          return { ...tab, pipelineRunId: mainPipelineId || undefined, agentId: mainAgentId }
        }
        return tab
      })
      activeTabId = saved.activeTabId || tabs[0].id
    } else {
      // 新会话：建主 Tab
      const mainTab: AgentTab = {
        id: `main-${sessionId}`,
        agentId: mainAgentId, agentName: '主Agent', agentLevel: 1,
        taskId: undefined, parentRecordId: undefined,
        pipelineRunId: mainPipelineId || undefined,
        path: ['主Agent'], status: 'running', hasUnread: false,
        canClose: false, messages: [],
      }
      tabs = [mainTab]
      activeTabId = mainTab.id
    }

    // 从 tabs 重建 pipelineTabMap（保证一致性）
    const newPipelineTabMap: Record<string, string> = {}
    for (const tab of tabs) {
      if (tab.pipelineRunId) {
        newPipelineTabMap[tab.pipelineRunId] = tab.id
      }
    }

    set({
      currentSessionId: sessionId,
      tabs,
      activeTabId,
      tabMessagesLoading: {},
      unreadCounts: {},
      pipelineTabMap: newPipelineTabMap,
    })

    // 激活当前活跃 Tab 对应的管道
    const activeTab = tabs.find((t) => t.id === activeTabId)
    if (activeTab?.pipelineRunId) {
      usePipelineMessageStore.getState().activatePipeline(activeTab.pipelineRunId)
    }

    // 子 Tab 懒加载：进会话时只注册 pipeline 元数据（无网络请求），
    // 不再 forEach 全量加载所有历史子 Tab 的消息。
    // 历史子 Tab 的消息在用户真正切换到该 Tab 时由 switchToTab/setActiveTab
    // 触发 loadTabMessages 加载（IndexedDB 有缓存时秒开，无缓存才发请求）。
    // 这样把进会话的并发消息请求从 N（历史累积的子 Tab 数）降到最多 1，
    // 避免会话切换瞬间打爆后端（曾出现 14 个子 Tab 并发 fetch 全部超时雪崩）。
    const pipelineStore = usePipelineMessageStore.getState()
    tabs.forEach((tab) => {
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
      }
    })

    // 仅当前活跃的子 Tab 触发消息加载（主管道由 sessionListStore.setActiveSession 负责加载）
    if (activeTab && activeTab.agentLevel !== 1 && activeTab.pipelineRunId) {
      get().loadTabMessages(activeTab.id)
    }
  },

  /** 保存当前标签状态到 localStorage（仅标签 + pipeline 映射）。 消息由 pipelineMessageStore 独立 persist，不在此缓存。 */
  saveCurrentTabs: () => {
    const { currentSessionId, tabs, activeTabId, pipelineTabMap } = get()
    if (!currentSessionId) return

    saveTabsToStorage(currentSessionId, tabs, activeTabId, pipelineTabMap)
  },

  /** 添加 Agent Tab */
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

  /** 移除 Agent Tab */
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

  /** 设置活跃 Tab（子 Tab 时自动从后端加载消息） */
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

  /** 更新 Tab 状态 */
  updateTabStatus: (tabId, status) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status } : t)),
    }))
  },

  /** 更新 Tab 未读状态 */
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

  /** 添加消息到指定 Tab（消息写入 pipelineMessageStore） */
  addMessageToTab: (tabId, message) => {
    const { tabs, pipelineTabMap, activeTabId } = get()
    const tab = tabs.find((t) => t.id === tabId)

    let pipelineId: string | null = null
    if (tab?.pipelineRunId) {
      pipelineId = tab.pipelineRunId
    } else {
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

  /** 获取当前活跃 Tab 的消息 从 pipelineMessageStore 读取 */
  getActiveTabMessages: () => {
    const { activeTabId, tabs, currentSessionId } = get()
    if (!activeTabId || !currentSessionId) return []
    const tab = tabs.find((t) => t.id === activeTabId)
    if (!tab) return []
    const pipelineId = tab.pipelineRunId
    if (!pipelineId) return []
    return usePipelineMessageStore.getState().getMessages(pipelineId)
  },

  /** 获取当前活跃 Tab */
  getActiveTab: () => {
    const { tabs, activeTabId } = get()
    return tabs.find((t) => t.id === activeTabId) || null
  },

  /** 清除 Tab 未读计数 */
  clearTabUnread: (tabId) => {
    set({
      unreadCounts: {
        ...get().unreadCounts,
        [tabId]: 0,
      },
      tabs: get().tabs.map((t) => (t.id === tabId ? { ...t, hasUnread: false } : t)),
    })
  },

  /** 重置所有 Tabs（会话切换时使用） */
  resetAllTabs: () => {
    set({
      tabs: [],
      activeTabId: null,
      tabMessagesLoading: {},
      unreadCounts: {},
      pipelineTabMap: {},
    })
  },

  /** 打开子 Tab */
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

  /** 关闭 Tab（增强版，支持主 Tab 保护，同时清理 pipeline 映射） */
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
        const mainPipelineId = getMainPipelineId(currentSessionId)
        if (mainPipelineId) {
          pipelineStore.activatePipeline(mainPipelineId)
          // 统一加载入口：仅当本地无消息时拉历史（mode='auto'，未初始化走全量）。
          // 不 await，保持 fire-and-forget 行为。
          if (!pipelineStore.messagesByPipeline[mainPipelineId]?.length) {
            void pipelineStore.loadPipelineMessages(mainPipelineId, { threadId: currentSessionId })
          }
        }
      }
    }
  },

  /** 切换子 Tab（增强版，自动清除未读，激活对应管道） */
  switchToTab: (tabId) => {
    const { tabs, activeTabId: prevActiveTabId } = get()
    const tab = tabs.find((t) => t.id === tabId)

    if (!tab) {
      console.warn(`[AgentTabStore] Tab not found: ${tabId}`)
      return
    }

    if (prevActiveTabId === tabId) return

    const pipelineStore = usePipelineMessageStore.getState()
    const effectivePipelineId = tab.pipelineRunId
    if (!effectivePipelineId) {
      console.error('[switchToTab] Tab 数据损坏：pipelineRunId 为空，中止切换: tabId=%s', tabId)
      return  // 中止切换，避免用错误 pipelineId 路由
    }
    pipelineStore.activatePipeline(effectivePipelineId)

    set({ activeTabId: tabId })
    get().clearTabUnread(tabId)

    // 切换标签时主动从后端拉取最新消息状态
    // 确保后台管道在切标签期间产生的新消息能被显示
    get().loadTabMessages(tabId)

    // 刷新页面后恢复到错误的标签（如上次选中的子标签而非当前主标签）。
    get().saveCurrentTabs()
  },

  /** 标记 Tab 完成 */
  markTabComplete: (tabId) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, status: 'completed' } : t)),
    }))
  },

  /** 合并到主 Tab（子 Tab 完成后） 从 pipelineMessageStore 读取消息并合并 */
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

  /** 清除未读（别名） */
  clearUnread: (tabId) => {
    get().clearTabUnread(tabId)
  },

  /** 更新 Tab 状态（别名） */
  updateTab: (tabId, updates) => {
    set((state) => ({
      tabs: state.tabs.map((t) => (t.id === tabId ? { ...t, ...updates } : t)),
    }))
  },

  /** 打开子 Agent Tab（统一接口） */
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
        const oldPipelineId = existingTab.pipelineRunId
        const updatedTab: AgentTab = {
          ...existingTab,
          status,
          pipelineRunId: pipelineId || existingTab.pipelineRunId,
          agentName: agentName || existingTab.agentName,
          taskId: taskId || existingTab.taskId,
          agentLevel: agentLevel || existingTab.agentLevel,
        }
        const tabsUpdate = state.tabs.map((t) => (t.id === tabId ? updatedTab : t))
        const mapUpdate = { ...state.pipelineTabMap }
        if (pipelineId) {
          mapUpdate[pipelineId] = tabId
        }
        if (oldPipelineId && oldPipelineId !== pipelineId && mapUpdate[oldPipelineId] === tabId) {
          delete mapUpdate[oldPipelineId]
        }
        return {
          tabs: tabsUpdate,
          pipelineTabMap: mapUpdate,
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

  /** 注册 pipeline_id → tabId 映射（注册后自动持久化） */
  registerPipelineTab: (pipelineId, tabId) => {
    set((state) => ({
      pipelineTabMap: {
        ...state.pipelineTabMap,
        [pipelineId]: tabId,
      },
    }))
    get().saveCurrentTabs()
  },

  /** 根据 pipeline_id 查找对应的 tabId */
  getTabIdByPipeline: (pipelineId) => {
    return get().pipelineTabMap[pipelineId]
  },

  /** 从后端 API 加载子 Tab 消息（持久化恢复） 历史修复记录（逻辑已被 pipelineStore.fetchMessages 替代）： */
  loadTabMessages: async (tabId, pipelineRunId) => {
    const state = get()

    // 防止并发加载
    if (state.tabMessagesLoading[tabId]) return

    const tab = state.tabs.find((t) => t.id === tabId)
    if (!tab || !state.currentSessionId) return

    const effectivePipelineId = pipelineRunId || tab.pipelineRunId
    if (!effectivePipelineId) {
      console.error('[loadTabMessages] Tab 数据损坏：pipelineRunId 为空，跳过加载: tabId=%s', tabId)
      return
    }

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

      // 统一加载入口：流式保护 + 双游标决策收敛到 loadPipelineMessages，
      // 子 Tab 切换也享受增量补漏（mode='auto'），与主会话切换行为一致。
      const result = await pipelineStore.loadPipelineMessages(effectivePipelineId, {
        threadId: state.currentSessionId,
      })
      if (!result.ok) {
        const error = result.error as any
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
      }

      if (get().activeTabId === tabId) {
        pipelineStore.activatePipeline(effectivePipelineId)
      }

      get().saveCurrentTabs()
    } finally {
      set((s) => ({
        tabMessagesLoading: { ...s.tabMessagesLoading, [tabId]: false },
      }))
    }
  },
}))
