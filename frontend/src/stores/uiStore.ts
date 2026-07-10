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
   */
  toggleWorkspace: () => {
    set((state) => {
      const newCollapsed = !state.workspaceCollapsed
      uiStorage.setWorkspaceCollapsed(newCollapsed)
      return { workspaceCollapsed: newCollapsed }
    })
  },
  /**
   * 设置工作区面板状态
   */
  setWorkspaceCollapsed: (collapsed: boolean) => {
    uiStorage.setWorkspaceCollapsed(collapsed)
    set({ workspaceCollapsed: collapsed })
  },
  setMessageSearchQuery: (query: string) => {
    set({ messageSearchQuery: query })
  },
}))
