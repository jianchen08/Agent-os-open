/**
 * Layout Mode Store
 *
 * Manages the toggle between the current chat layout and the five-space layout.
 * Also manages the state of floating windows, workspace tabs, and dock items
 * that belong to the five-space layout.
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
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

  /**
   * 已「访问过」（至少激活过一次）的工作区 Tab ID 集合
   *
   * PERF-fix_20260628_workspace_tab_freeze:
   * 问题根因: WorkspacePanel 此前为修「切换 Tab 丢状态」bug，把所有 Tab 的真实
   *   内容都挂载，非激活的用 hidden 隐藏。但 display:none 只省布局，不省 JS 执行
   *   和 DOM 构建。叠加 workspaceTabs 持久化，刷新后所有曾打开的 Tab 全量恢复
   *   并一次性挂载——N 个 FileTreeWidget（mount 即发 /file-tree）、CodeEditor
   *   （SyntaxHighlighter 全量高亮）、HtmlPreviewWidget iframe 同时跑，主线程
   *   被占满 → 加载卡死/白屏。
   * 修复方案: WorkspacePanel 改为「懒挂载 + 已访问保活」——只有当前激活 Tab 或
   *   曾经访问过的 Tab 才渲染真实内容，其余 Tab 不挂载。visitedTabIds 记录哪些
   *   Tab 已访问过，供 WorkspacePanel 判断是否需要渲染。
   * 不持久化: 刷新即视为「重新开始」，只挂载激活 Tab = 性能最优。用户重新点开
   *   其它 Tab 时加载一次（各组件内部已有 loading 态），点开后保活。
   */
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
  updateWorkspaceTab: (tabId: string, updates: Partial<WorkspaceTab>) => void

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

export const useLayoutModeStore = create<LayoutModeState & LayoutModeActions>()(
  persist(
    (set) => ({
      // Layout mode
      mode: 'classic',

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
      connectionStatus: {
        state: 'disconnected',
        latencyMs: null,
        reconnectAttempt: 0,
        lastConnectedAt: null,
        queuedMessages: 0,
      },
      workspaceDataVersion: 0,
      // PERF-fix_20260628_workspace_tab_freeze: 不持久化，刷新后重置为空
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

      // BUG-FIX-fix_20260512_tab_not_switching:
      // 问题根因: addWorkspaceTab 只追加 tab 到数组，不将其他 tab 的 isActive 设为 false，
      //          导致多个 tab 同时 isActive=true，WorkspacePanel 用 find 取第一个匹配，
      //          内容区渲染旧 tab 内容而新 tab 样式显示为选中。
      // 修复方案: 新 tab 的 isActive 为 true 时，自动将其他 tab 设为 false。
      //
      // PERF-fix_20260628_workspace_tab_freeze: 新 tab 若为激活态，同时并入 visitedTabIds，
      //   保证懒挂载策略下激活 Tab 立即可见（首屏渲染的关键来源）。
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
      // PERF-fix_20260628_workspace_tab_freeze: 激活 Tab 并入 visitedTabIds，
      //   使其真实内容被渲染（首次访问触发懒挂载）。
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
      // PERF-fix_20260628_workspace_tab_freeze: 关闭 Tab 时清理 visited 记录，
      //   下次若重开会重新挂载（其内部状态本就随卸载丢失，记录保留无意义）。
      closeWorkspaceTab: (tabId) =>
        set((state) => ({
          workspaceTabs: state.workspaceTabs.filter((t) => t.id !== tabId),
          visitedTabIds: state.visitedTabIds.filter((id) => id !== tabId),
        })),
      updateWorkspaceTab: (tabId, updates) =>
        set((state) => ({
          workspaceTabs: state.workspaceTabs.map((t) =>
            t.id === tabId ? { ...t, ...updates } : t,
          ),
        })),

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
      // 配额满时吞掉 QuotaExceededError，避免 toggleMode 等 action 崩溃
      storage: createTolerantStorage(),
      // BUG-FIX-fix_20260625_workspace_tabs_persist:
      // 问题根因: 此前 partialize 只保存 mode，workspaceTabs 整组刷新即丢，
      //          所有打开的文件/工具 Tab 都需重新打开，体验差。
      // 修复方案: 持久化 workspaceTabs；运行时状态（floatingWindows、dockItems、
      //          activeExecutions、pendingInteractions、connectionStatus、fullscreen）
      //          在 merge 时强制重置，避免恢复到一个不一致的状态。
      partialize: (state) => ({
        mode: state.mode,
        workspaceTabs: state.workspaceTabs,
      }),
      merge: (persisted, current) => {
        const p = (persisted as Partial<LayoutModeState>) || {}
        const tabs = Array.isArray(p.workspaceTabs) ? p.workspaceTabs : current.workspaceTabs
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
