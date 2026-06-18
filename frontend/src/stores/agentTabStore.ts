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
 * BUG-FIX-fix_20260605_main_pipeline_use_pipeline_ids_first:
 * 问题根因: session.activePipelineId 是"最近激活的管道"，派生过子 Tab 后这个值
 *          会变成子管道 ID（如 "流程优化" 会话：pipelineIds=[主, 子]，
 *          activePipelineId=子）。如果继续用 activePipelineId 作为"主 Tab 管道 ID"，
 *          主 Tab 会加载到子管道的消息，表现为"主 Tab 显示子任务消息"。
 * 修复方案: 主管道按创建顺序固定为 session.pipelineIds[0]（派生子 Tab 时不会改变
 *          已有的管道顺序）。仅当 pipelineIds 为空时才 fallback 到 activePipelineId。
 * 影响范围: 派生过子 Tab 的会话切换 / 主 Tab 消息显示
 * 修复日期: 2026-06-05
 */
function getMainPipelineId(sessionId: string): string | null {
  const sessions = useSessionStore.getState().sessions
  const session = sessions.find((s) => s.id === sessionId)
  // BUG-FIX-fix_20260617_remove_main_pipeline_fallback:
  // 问题根因: 原代码用 session.activePipelineId 作为主管道 fallback，
  //          但 activePipelineId 在派生过子 Tab 时会指向子管道，作为主管道兜底是语义错误。
  // 修复方案: 只用 pipelineIds[0]，缺失时记 warn 返回 null 由调用方处理。
  // 影响范围: 主管道 ID 解析
  // 修复日期: 2026-06-17
  const mainPid = session?.pipelineIds?.[0]
  if (!mainPid) {
    console.warn('[getMainPipelineId] 主管道缺失: sessionId=%s pipelineIds=%o', sessionId, session?.pipelineIds)
  }
  return mainPid ?? null
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
   *
   * 核心规则：activeTab.pipelineRunId 是加载管道的唯一依据。
   * 主管道（agentLevel=1）的 pipelineRunId 永远等于 getMainPipelineId(sessionId)，
   * 与 setActiveSession 中 fetchMessages 用的 pipelineId 保持一致。
   * 不再从 pipelineTabMap 反向补全，避免脏数据导致主 Tab 错拿子 Tab 的 pipelineRunId。
   */
  initSessionTabs: (sessionId) => {
    const saved = loadTabsFromStorage(sessionId)
    const mainPipelineId = getMainPipelineId(sessionId)

    let tabs: AgentTab[]
    let activeTabId: string | null

    if (saved && saved.tabs.length > 0) {
      // 主管道 pipelineRunId 始终用 session 提供的最新 ID（与 fetchMessages 一致）
      // 子 Tab 缺 pipelineRunId 时保持 undefined（不污染）
      tabs = saved.tabs.map((tab) => {
        if (tab.agentLevel === 1) {
          return { ...tab, pipelineRunId: mainPipelineId || undefined }
        }
        return tab
      })
      activeTabId = saved.activeTabId || tabs[0].id
    } else {
      // 新会话：建主 Tab
      const mainTab: AgentTab = {
        id: `main-${sessionId}`,
        agentId: '', agentName: '主Agent', agentLevel: 1,
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
      tabMessages: {},
      tabMessagesLoading: {},
      unreadCounts: {},
      pipelineTabMap: newPipelineTabMap,
    })

    // 激活当前活跃 Tab 对应的管道
    const activeTab = tabs.find((t) => t.id === activeTabId)
    if (activeTab?.pipelineRunId) {
      usePipelineMessageStore.getState().activatePipeline(activeTab.pipelineRunId)
    }

    // 子 Tab 异步加载消息
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
        get().loadTabMessages(tab.id)
      }
    })
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
      const pid = tab.pipelineRunId
      if (pid) {
        const msgs = pipelineStore.getMessages(pid)
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

  /**
   * 获取当前活跃 Tab 的消息
   * 从 pipelineMessageStore 读取
   */
  getActiveTabMessages: () => {
    const { activeTabId, tabs, currentSessionId } = get()
    if (!activeTabId || !currentSessionId) return []
    const tab = tabs.find((t) => t.id === activeTabId)
    if (!tab) return []
    const pipelineId = tab.pipelineRunId
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
    // BUG-FIX-fix_20260617_remove_pipeline_fallback:
    // 问题根因: tab.pipelineRunId 为空时从 pipelineTabMap 反向查找是脏数据兜底，
    //          会用错误的 pipelineId 路由到错误管道。Tab 数据不完整本就是 bug，应报错。
    // 修复方案: 直接用 tab.pipelineRunId，缺失时记 error 并中止切换，不写入 store。
    // 影响范围: Tab 切换路径
    // 修复日期: 2026-06-17
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

      // BUG-FIX-fix_20260528_tab_pipeline_mapping:
      // 问题根因: 父Agent连续派生多个子Agent时，相同 parentRecordId 复用同一个 Tab，
      //          但 existingTab 分支只更新 status，不更新 pipelineRunId，
      //          导致 Tab 指向旧管道，switchToTab 激活错误的管道。
      // 修复方案: existingTab 分支同步更新 pipelineRunId/agentName/taskId/agentLevel，
      //          并清理旧 pipelineTabMap 映射，防止残留映射干扰路由。
      // 影响范围: 多轮子Agent派生场景下的Tab→Pipeline映射准确性
      // 修复日期: 2026-05-28
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

    // BUG-FIX-fix_20260617_remove_pipeline_fallback:
    // 问题根因: 同 switchToTab，pipelineTabMap 反向查找是脏数据兜底，
    //          会用错误的 pipelineId 加载错误管道的消息。
    // 修复方案: 直接用 pipelineRunId 参数或 tab.pipelineRunId，缺失时报错返回。
    // 影响范围: Tab 消息加载路径
    // 修复日期: 2026-06-17
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

      // BUG-FIX-fix_20260515_streaming_interrupt:
      // 问题根因: 切换标签/会话时 loadTabMessages 无条件调用 fetchMessages -> initFromAPI，
      //          后端 API 返回的数据（可能是已完成状态）会覆盖正在流式传输的消息，
      //          导致流式输出中断、内容丢失、状态错误。
      // 修复方案: 管道正在流式传输时跳过 API 请求，保留本地的流式数据。
      //          流式数据由 WebSocket 全局事件处理器持续更新，不受组件挂载/卸载影响。
      //          切换回来时 activatePipeline 即可恢复显示，无需重新获取。
      // 影响范围: 标签切换、会话切换时的流式输出连续性
      // 修复日期: 2026-05-15
      //
      // BUG-FIX-fix_20260614_stale_streaming_blocks_history:
      // 问题根因: 与 sessionListStore.setActiveSession 不同，此处无 existingCount 兜底。
      //          当 isStreaming 卡 true（stale，如后端崩溃未正确发 stream_end）时，
      //          切换 tab 永远不拉历史，用户看到"加载了记录但没显示"（bug 1）。
      // 修复方案: 补 existingCount <= 1 兜底，与 sessionListStore 对齐。
      const _existingCount = (pipelineStore.messagesByPipeline[effectivePipelineId] || []).length
      if (!pipelineStore.isStreaming(effectivePipelineId) || _existingCount <= 1) {
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
