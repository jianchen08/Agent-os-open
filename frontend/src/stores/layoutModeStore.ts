/** Layout Mode Store Manages the toggle between the current chat layout and the five-space layout. */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import type { PersistStorage, StorageValue } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import { loggers } from '@/utils/logger'
import type { FloatingWindowInstance, WorkspaceTab, DockItem } from '@/types/layout'
import type { ReactNode } from 'react'

/** Layout mode type */
export type LayoutMode = 'classic' | 'five-space'

/** Execution event data for real-time display */
export interface ExecutionEvent {
  id: string
  type: 'tool' | 'agent' | 'workflow'
  name: string
  status: 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  startedAt: string
  completedAt?: string
  output?: string
  error?: string
}

/** Interaction request data */
export interface InteractionRequest {
  id: string
  executionId: string
  prompt: string
  timeout?: number
  timestamp: string
}

/** Connection status detail */
export interface ConnectionStatus {
  state: 'connected' | 'connecting' | 'reconnecting' | 'disconnected' | 'failed'
  latencyMs: number | null
  reconnectAttempt: number
  lastConnectedAt: string | null
  queuedMessages: number
}

interface LayoutModeState {
  /** Current layout mode */
  mode: LayoutMode

  /** Floating window instances */
  floatingWindows: FloatingWindowInstance[]

  /** Workspace tabs */
  workspaceTabs: WorkspaceTab[]

  /** Dock items */
  dockItems: DockItem[]

  /** Fullscreen overlay state */
  fullscreenActive: boolean
  fullscreenTitle: string | null
  fullscreenContent: ReactNode | null

  /** Active executions (for dock bar status) */
  activeExecutions: ExecutionEvent[]

  /** Pending interaction requests */
  pendingInteractions: InteractionRequest[]

  /** Connection status */
  connectionStatus: ConnectionStatus

  /** 工作区数据版本号，每次 bump 时递增，驱动 FileTreeWidget 等组件重新加载 */
  workspaceDataVersion: number

 /** 已「访问过」（至少激活过一次）的工作区 Tab ID 集合 PERF */
  visitedTabIds: string[]
}

interface LayoutModeActions {
  /** Toggle layout mode */
  toggleMode: () => void
  /** Set specific layout mode */
  setMode: (mode: LayoutMode) => void

  /** Floating window management */
  addFloatingWindow: (window: FloatingWindowInstance) => void
  updateFloatingWindow: (id: string, updates: Partial<FloatingWindowInstance>) => void
  closeFloatingWindow: (id: string) => void
  minimizeFloatingWindow: (id: string) => void
  restoreFloatingWindow: (id: string) => void

  /** Workspace tab management */
  addWorkspaceTab: (tab: WorkspaceTab) => void
  setActiveTab: (tabId: string) => void
  closeWorkspaceTab: (tabId: string) => void
  /** 关闭指定标签外的其他标签（钉住标签保留） */
  closeOtherWorkspaceTabs: (tabId: string) => void
  /** 关闭全部标签（钉住标签保留） */
  closeAllWorkspaceTabs: () => void
  updateWorkspaceTab: (tabId: string, updates: Partial<WorkspaceTab>) => void
  /** 拖拽换位：把 dragTabId 移到 targetTabId 当前位置 */
  reorderWorkspaceTabs: (dragTabId: string, targetTabId: string) => void

  /** Dock item management */
  setDockItems: (items: DockItem[]) => void
  updateDockItem: (id: string, updates: Partial<DockItem>) => void

  /** Fullscreen overlay management */
  enterFullscreen: (title: string, content: ReactNode) => void
  exitFullscreen: () => void

  /** Execution event management */
  addOrUpdateExecution: (event: ExecutionEvent) => void
  removeExecution: (id: string) => void
  clearCompletedExecutions: () => void

  /** Interaction request management */
  addInteraction: (request: InteractionRequest) => void
  removeInteraction: (id: string) => void

  /** Connection status management */
  updateConnectionStatus: (status: Partial<ConnectionStatus>) => void

  /** 递增工作区数据版本号，触发依赖组件刷新 */
  bumpWorkspaceDataVersion: () => void
}

// ---- persist 缩容闸（BUG-79）----
// layout-mode 在每次 set 时整体覆写持久化的 workspaceTabs：任何把页签集合写小的
// 路径都会被持久化放大成不可逆丢失（12 个用户签被覆盖写没）。闸门契约：页签集合
// 缩小的持久化写入仅允许两类来源——
// ① 用户关签动作（closeWorkspaceTab/closeOtherWorkspaceTabs/closeAllWorkspaceTabs，
//    同步事置 userClosePending，与随后的本次写 1:1 消费）；
// ② merge 迁移清洗（登记本次清洗移除的页签 id，写入丢失集 ⊆ 移除集时放行，
//    清洗与后续加签同窗发生时仍可落盘）。
// 其余缩容写入一律拒写并告警：localStorage 保留上次完好集合，刷新后经 merge
// 原样恢复（自愈，无需恢复集）。
let userClosePending = false
let migrationRemovedTabIds: string[] | null = null

/** persist 落盘的页签载荷形状（partialize 产物） */
type PersistedLayoutState = { mode: LayoutMode; workspaceTabs: WorkspaceTab[] }

function extractTabIds(value: StorageValue<PersistedLayoutState> | null): string[] | null {
  const tabs = value?.state?.workspaceTabs
  if (!Array.isArray(tabs)) return null
  const ids: string[] = []
  for (const tab of tabs) {
    if (typeof (tab as WorkspaceTab)?.id === 'string') ids.push(tab.id as string)
  }
  return ids
}

function createShrinkGuardedLayoutStorage(): PersistStorage<PersistedLayoutState> | undefined {
  const inner = createTolerantStorage()
  if (!inner) return inner
  return {
    getItem: (name) =>
      inner.getItem(name) as
        | StorageValue<PersistedLayoutState>
        | null
        | Promise<StorageValue<PersistedLayoutState> | null>,
    setItem: (name, value) => {
      if (name === 'layout-mode') {
        const nextIds = extractTabIds(value)
        if (nextIds !== null) {
          const prevIds = extractTabIds(inner.getItem(name) as StorageValue<PersistedLayoutState> | null)
          if (prevIds !== null) {
            const lost = prevIds.filter((id) => !nextIds.includes(id))
            const removed = migrationRemovedTabIds
            const migrationAuthorized =
              removed !== null && lost.length > 0 && lost.every((id) => removed.includes(id))
            if (lost.length > 0 && !userClosePending && !migrationAuthorized) {
              loggers.storage.warn(
                '[layout-mode] 拒绝缩容页签集的持久化写入（非关签/迁移来源，BUG-79 闸）：'
                  + '丢失 %d 签 %j，保留上次完好集合，刷新后自动恢复',
                lost.length,
                lost,
              )
              return
            }
          }
        }
        userClosePending = false
        migrationRemovedTabIds = null
      }
      inner.setItem(name, value)
    },
    removeItem: (name) => inner.removeItem(name),
  }
}

export const useLayoutModeStore = create<LayoutModeState & LayoutModeActions>()(
  persist(
    (set) => ({
      // Layout mode — Deep Space App Shell (TitleBar 32 / SideBar / StatusBar 22)
      mode: 'five-space',

      // Five-space layout state
      floatingWindows: [],
      workspaceTabs: [],
      dockItems: [],
      fullscreenActive: false,
      fullscreenTitle: null,
      fullscreenContent: null,

      // Real-time data
      activeExecutions: [],
      pendingInteractions: [],
      // 初始 'connecting'：刷新后尚未发起首次连接，「从未连接」≠「断开」，
      // 避免 AlertBanner 在首连期间弹"内核连接已断开"误导横幅（叠加 4s
      // 解除保留期 = 用户感知的"刷新后 5-10s 才连上"）。
      connectionStatus: {
        state: 'connecting',
        latencyMs: null,
        reconnectAttempt: 0,
        lastConnectedAt: null,
        queuedMessages: 0,
      },
      workspaceDataVersion: 0,
 // PERF 不持久化，刷新后重置为空
      visitedTabIds: [],

      // Actions
      toggleMode: () => set((state) => ({ mode: state.mode === 'classic' ? 'five-space' : 'classic' })),
      setMode: (mode) => set({ mode }),

      addFloatingWindow: (window) =>
        set((state) => ({ floatingWindows: [...state.floatingWindows, window] })),
      updateFloatingWindow: (id, updates) =>
        set((state) => ({
          floatingWindows: state.floatingWindows.map((w) =>
            w.id === id ? { ...w, ...updates } : w,
          ),
        })),
      closeFloatingWindow: (id) =>
        set((state) => ({
          floatingWindows: state.floatingWindows.filter((w) => w.id !== id),
        })),
      minimizeFloatingWindow: (id) =>
        set((state) => ({
          floatingWindows: state.floatingWindows.map((w) =>
            w.id === id ? { ...w, isMinimized: true } : w,
          ),
        })),
      restoreFloatingWindow: (id) =>
        set((state) => ({
          floatingWindows: state.floatingWindows.map((w) =>
            w.id === id ? { ...w, isMinimized: false } : w,
          ),
        })),

      // 内容区渲染旧 tab 内容而新 tab 样式显示为选中。
      // 保证懒挂载策略下激活 Tab 立即可见（首屏渲染的关键来源）。
      addWorkspaceTab: (tab) =>
        set((state) => ({
          workspaceTabs: [
            ...state.workspaceTabs.map((t) =>
              tab.isActive ? { ...t, isActive: false } : t,
            ),
            tab,
          ],
          visitedTabIds:
            tab.isActive && !state.visitedTabIds.includes(tab.id)
              ? [...state.visitedTabIds, tab.id]
              : state.visitedTabIds,
        })),
 // PERF 激活 Tab 并入 visitedTabIds，
      // 使其真实内容被渲染（首次访问触发懒挂载）。
      setActiveTab: (tabId) =>
        set((state) => ({
          workspaceTabs: state.workspaceTabs.map((t) => ({
            ...t,
            isActive: t.id === tabId,
          })),
          visitedTabIds: state.visitedTabIds.includes(tabId)
            ? state.visitedTabIds
            : [...state.visitedTabIds, tabId],
        })),
 // PERF 关闭 Tab 时清理 visited 记录，
      // 下次若重开会重新挂载（其内部状态本就随卸载丢失，记录保留无意义）。
      closeWorkspaceTab: (tabId) => {
        // 关签意图随本次写消费：缩容闸据此放行用户主动关签的持久化（BUG-79 闸）
        userClosePending = true
        set((state) => ({
          workspaceTabs: state.workspaceTabs.filter((t) => t.id !== tabId),
          visitedTabIds: state.visitedTabIds.filter((id) => id !== tabId),
        }))
      },
      closeOtherWorkspaceTabs: (tabId) => {
        userClosePending = true
        set((state) => {
          const keep = state.workspaceTabs.filter(
            (t) => t.id === tabId || t.isPinned,
          )
          // 激活标签被关时激活第一个剩余标签（钉住优先）
          let tabs = keep
          if (!keep.some((t) => t.isActive) && keep.length > 0) {
            tabs = keep.map((t, i) => (i === 0 ? { ...t, isActive: true } : t))
          }
          return {
            workspaceTabs: tabs,
            visitedTabIds: state.visitedTabIds.filter(
              (id) => tabs.some((t) => t.id === id),
            ),
          }
        })
      },
      closeAllWorkspaceTabs: () => {
        userClosePending = true
        set((state) => {
          const keep = state.workspaceTabs.filter((t) => t.isPinned)
          return {
            workspaceTabs: keep,
            visitedTabIds: state.visitedTabIds.filter(
              (id) => keep.some((t) => t.id === id),
            ),
          }
        })
      },
      updateWorkspaceTab: (tabId, updates) =>
        set((state) => ({
          workspaceTabs: state.workspaceTabs.map((t) =>
            t.id === tabId ? { ...t, ...updates } : t,
          ),
        })),

      reorderWorkspaceTabs: (dragTabId, targetTabId) =>
        set((state) => {
          if (dragTabId === targetTabId) return state
          const from = state.workspaceTabs.findIndex((t) => t.id === dragTabId)
          const to = state.workspaceTabs.findIndex((t) => t.id === targetTabId)
          if (from === -1 || to === -1) return state
          const tabs = [...state.workspaceTabs]
          const [moved] = tabs.splice(from, 1)
          tabs.splice(to, 0, moved)
          return { workspaceTabs: tabs }
        }),

      setDockItems: (items) => set({ dockItems: items }),
      updateDockItem: (id, updates) =>
        set((state) => ({
          dockItems: state.dockItems.map((item) =>
            item.id === id ? { ...item, ...updates } : item,
          ),
        })),

      enterFullscreen: (title, content) =>
        set({ fullscreenActive: true, fullscreenTitle: title, fullscreenContent: content }),
      exitFullscreen: () =>
        set({ fullscreenActive: false, fullscreenTitle: null, fullscreenContent: null }),

      addOrUpdateExecution: (event) =>
        set((state) => {
          const existingIndex = state.activeExecutions.findIndex((e) => e.id === event.id)
          if (existingIndex >= 0) {
            const updated = [...state.activeExecutions]
            updated[existingIndex] = event
            return { activeExecutions: updated }
          }
          return { activeExecutions: [...state.activeExecutions, event] }
        }),
      removeExecution: (id) =>
        set((state) => ({
          activeExecutions: state.activeExecutions.filter((e) => e.id !== id),
        })),
      clearCompletedExecutions: () =>
        set((state) => ({
          activeExecutions: state.activeExecutions.filter(
            (e) => e.status === 'running',
          ),
        })),

      addInteraction: (request) =>
        set((state) => ({
          pendingInteractions: [...state.pendingInteractions, request],
        })),
      removeInteraction: (id) =>
        set((state) => ({
          pendingInteractions: state.pendingInteractions.filter((r) => r.id !== id),
        })),

      updateConnectionStatus: (status) =>
        set((state) => ({
          connectionStatus: { ...state.connectionStatus, ...status },
        })),

      bumpWorkspaceDataVersion: () =>
        set((state) => ({ workspaceDataVersion: state.workspaceDataVersion + 1 })),
    }),
    {
      name: 'layout-mode',
      // 配额满时吞掉 QuotaExceededError（含缩容闸：非关签/迁移来源的缩容写入拒落盘）
      storage: createShrinkGuardedLayoutStorage(),
      // 在 merge 时强制重置，避免恢复到一个不一致的状态。
      partialize: (state) => ({
        mode: state.mode,
        workspaceTabs: state.workspaceTabs,
      }),
      merge: (persisted, current) => {
        const p = (persisted as Partial<LayoutModeState>) || {}
        let tabs = Array.isArray(p.workspaceTabs) ? p.workspaceTabs : current.workspaceTabs
        const preCleanTabIds = tabs.map((t) => t.id)
        // 迁移（0.2 收尾）：旧的固定「工作区」标签（ws-panel-workspace）已退役——
        // 右侧面板本身就是工作区，顶层标签（任务管理/文件树/设置…）都是其内容，
        // 不再存在名为「工作区」的标签页。从持久化数据中清洗掉。
        tabs = tabs.filter((t) => t.id !== 'ws-panel-workspace')
        // 迁移（plugins_panel 撤除）：独立「插件管理」面板与设置中枢 kernel-plugins
        // 双入口收敛，注册名已摘——持久化的旧页签从持久化数据中清洗掉。
        tabs = tabs.filter((t) => t.id !== 'ws-panel-plugins')
        // 迁移（任务管理归前端 2026-09-21 用户裁定）：任务管理页不再由 task_service
        // 插件的 contributes.pages 声明（页面归前端，声明在前端预置表），旧页签 id
        // ws-plugin-tasks 退役——清洗后由预置面板以 ws-panel-tasks 重新打开，避免
        // 同一功能在顶带残留两个页签。
        tabs = tabs.filter((t) => t.id !== 'ws-plugin-tasks')
        // 清洗后为空 → 默认激活任务管理标签（面板直接展示任务管理）
        if (tabs.length === 0) {
          tabs = [
            {
              id: 'ws-panel-tasks',
              title: '任务管理',
              icon: 'folder',
              moduleId: '__panel_tasks__',
              component: 'pipeline_manager',
              isActive: true,
              isPinned: false,
            },
          ]
        }
        // 迁移清洗授权：登记本次清洗移除的页签 id，缩容闸仅对丢失集 ⊆ 移除集的
        // 写入放行（BUG-79 闸；清洗与后续加签同窗发生时仍可落盘）
        migrationRemovedTabIds = preCleanTabIds.filter(
          (id) => !tabs.some((t) => t.id === id),
        )
        return {
          ...current,
          ...p,
          workspaceTabs: tabs,
          // 运行时状态强制重置
          floatingWindows: [],
          dockItems: [],
          fullscreenActive: false,
          fullscreenTitle: null,
          fullscreenContent: null,
          activeExecutions: [],
          pendingInteractions: [],
          connectionStatus: { ...current.connectionStatus },
          workspaceDataVersion: 0,
        }
      },
    },
  ),
)
