/**
 * UI 状态管理 Store
 *
 * 管理非主题相关的 UI 状态（侧边栏、审批对话框等）
 * 主题管理已移至 themeStore
 */

import { create } from 'zustand'
import { uiStorage } from '@/utils/storage'
import type { ApprovalRequest } from '@/types/models'

/**
 * 从 localStorage 读取初始折叠状态，读取失败时回退为默认值
 */
function loadCollapsedState(
  getter: () => boolean | null,
  fallback: boolean,
): boolean {
  try {
    const stored = getter()
    return stored !== null ? stored : fallback
  } catch {
    return fallback
  }
}

/**
 * 从 localStorage 读取工作区面板宽度比例，非法或未设置时回退 null（表示用默认比例）
 */
function loadWorkspacePanelRatio(): number | null {
  try {
    return uiStorage.getWorkspacePanelRatio()
  } catch {
    return null
  }
}

/**
 * UI 状态接口
 */
interface UIState {
  /** 侧边栏是否折叠 */
  sidebarCollapsed: boolean
  /** 审批对话框数据 */
  approvalDialog: ApprovalRequest | null
  /** 任务状态面板是否折叠 */
  taskPanelCollapsed: boolean
  /** 工作区面板是否折叠 */
  workspaceCollapsed: boolean
  /** 工作区面板是否最大化（保留顶栏/状态栏，仅折叠侧栏+聊天） */
  workspaceMaximized: boolean
  /** 工作区面板宽度比例（0~1，相对 chat+workspace）；null 表示用默认比例 */
  workspacePanelRatio: number | null
  /** 侧边栏宽度比例（0~1，相对主内容区）；null 表示用默认比例 */
  sidebarRatio: number | null
  /** 消息搜索关键词（Sidebar 与 ChatContainer 共享） */
  messageSearchQuery: string
}

interface UIActions {
  /** 切换侧边栏 */
  toggleSidebar: () => void
  /** 设置侧边栏状态 */
  setSidebarCollapsed: (collapsed: boolean) => void
  /** 显示审批对话框 */
  showApprovalDialog: (approval: ApprovalRequest) => void
  /** 隐藏审批对话框 */
  hideApprovalDialog: () => void
  /** 切换任务状态面板 */
  toggleTaskPanel: () => void
  /** 设置任务状态面板状态 */
  setTaskPanelCollapsed: (collapsed: boolean) => void
  /** 切换工作区面板 */
  toggleWorkspace: () => void
  /** 设置工作区面板状态 */
  setWorkspaceCollapsed: (collapsed: boolean) => void
  /** 切换工作区面板最大化（保留顶栏/状态栏，仅折叠侧栏+聊天） */
  toggleWorkspaceMaximize: () => void
  /** 设置工作区面板最大化状态 */
  setWorkspaceMaximized: (maximized: boolean) => void
  /** 设置工作区面板宽度比例（null 表示用默认比例） */
  setWorkspacePanelRatio: (ratio: number | null) => void
  /** 设置侧边栏宽度比例（null 表示用默认比例） */
  setSidebarRatio: (ratio: number | null) => void
  /** 设置消息搜索关键词 */
  setMessageSearchQuery: (query: string) => void
}

/**
 * UI Store
 *
 * 折叠状态在 store 创建时直接从 localStorage 读取初始值，
 * 无需依赖外部调用 initializeUI()。
 */
export const useUIStore = create<UIState & UIActions>((set) => ({
  sidebarCollapsed: loadCollapsedState(uiStorage.getSidebarCollapsed, false),
  approvalDialog: null,
  taskPanelCollapsed: loadCollapsedState(uiStorage.getTaskPanelCollapsed, false),
  workspaceCollapsed: loadCollapsedState(uiStorage.getWorkspaceCollapsed, false),
  workspaceMaximized: loadCollapsedState(uiStorage.getWorkspaceMaximized, false),
  workspacePanelRatio: loadWorkspacePanelRatio(),
  sidebarRatio: (() => {
    try {
      return uiStorage.getSidebarRatio()
    } catch {
      return null
    }
  })(),
  messageSearchQuery: '',

  /**
   * 切换侧边栏折叠状态
   */
  toggleSidebar: () => {
    set((state) => {
      const newCollapsed = !state.sidebarCollapsed
      uiStorage.setSidebarCollapsed(newCollapsed)
      return { sidebarCollapsed: newCollapsed }
    })
  },

  /**
   * 设置侧边栏状态
   */
  setSidebarCollapsed: (collapsed: boolean) => {
    uiStorage.setSidebarCollapsed(collapsed)
    set({ sidebarCollapsed: collapsed })
  },

  /**
   * 显示审批对话框
   */
  showApprovalDialog: (approval: ApprovalRequest) => {
    set({ approvalDialog: approval })
  },

  /**
   * 隐藏审批对话框
   */
  hideApprovalDialog: () => {
    set({ approvalDialog: null })
  },

  /**
   * 切换任务状态面板折叠状态
   */
  toggleTaskPanel: () => {
    set((state) => {
      const newCollapsed = !state.taskPanelCollapsed
      uiStorage.setTaskPanelCollapsed(newCollapsed)
      return { taskPanelCollapsed: newCollapsed }
    })
  },
  /**
   * 设置任务状态面板状态
   */
  setTaskPanelCollapsed: (collapsed: boolean) => {
    uiStorage.setTaskPanelCollapsed(collapsed)
    set({ taskPanelCollapsed: collapsed })
  },
  /**
   * 切换工作区面板折叠状态
   * 折叠与最大化互斥：折叠工作区时退出最大化。
   */
  toggleWorkspace: () => {
    set((state) => {
      const newCollapsed = !state.workspaceCollapsed
      uiStorage.setWorkspaceCollapsed(newCollapsed)
      if (newCollapsed && state.workspaceMaximized) {
        uiStorage.setWorkspaceMaximized(false)
        return { workspaceCollapsed: newCollapsed, workspaceMaximized: false }
      }
      return { workspaceCollapsed: newCollapsed }
    })
  },
  /**
   * 设置工作区面板状态
   * 折叠与最大化互斥：折叠工作区时退出最大化。
   */
  setWorkspaceCollapsed: (collapsed: boolean) => {
    uiStorage.setWorkspaceCollapsed(collapsed)
    if (collapsed) {
      const cur = useUIStore.getState().workspaceMaximized
      if (cur) {
        uiStorage.setWorkspaceMaximized(false)
        set({ workspaceCollapsed: collapsed, workspaceMaximized: false })
        return
      }
    }
    set({ workspaceCollapsed: collapsed })
  },
  /**
   * 切换工作区面板最大化状态
   * 最大化与折叠互斥：最大化时取消折叠（工作区需要可见）。
   */
  toggleWorkspaceMaximize: () => {
    set((state) => {
      const newMaximized = !state.workspaceMaximized
      uiStorage.setWorkspaceMaximized(newMaximized)
      if (newMaximized && state.workspaceCollapsed) {
        uiStorage.setWorkspaceCollapsed(false)
        return { workspaceMaximized: newMaximized, workspaceCollapsed: false }
      }
      return { workspaceMaximized: newMaximized }
    })
  },
  /**
   * 设置工作区面板最大化状态
   * 最大化与折叠互斥：最大化时取消折叠（工作区需要可见）。
   */
  setWorkspaceMaximized: (maximized: boolean) => {
    uiStorage.setWorkspaceMaximized(maximized)
    if (maximized) {
      const cur = useUIStore.getState().workspaceCollapsed
      if (cur) {
        uiStorage.setWorkspaceCollapsed(false)
        set({ workspaceMaximized: maximized, workspaceCollapsed: false })
        return
      }
    }
    set({ workspaceMaximized: maximized })
  },
  /**
   * 设置工作区面板宽度比例（null 表示清除记忆，回退默认比例）
   */
  setWorkspacePanelRatio: (ratio: number | null) => {
    if (ratio === null) {
      uiStorage.setWorkspacePanelRatio(undefined)
    } else {
      uiStorage.setWorkspacePanelRatio(ratio)
    }
    set({ workspacePanelRatio: ratio })
  },
  setSidebarRatio: (ratio: number | null) => {
    if (ratio === null) {
      uiStorage.setSidebarRatio(undefined)
    } else {
      uiStorage.setSidebarRatio(ratio)
    }
    set({ sidebarRatio: ratio })
  },
  setMessageSearchQuery: (query: string) => {
    set({ messageSearchQuery: query })
  },
}))
