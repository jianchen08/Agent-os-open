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
 * 主题类型
 */
export type Theme = 'light' | 'dark'

/**
 * UI 状态接口
 */
interface UIState {
  /** 侧边栏是否折叠 */
  sidebarCollapsed: boolean
  /** 审批对话框数据 */
  approvalDialog: ApprovalRequest | null
  /** 执行图面板是否折叠 */
  executionGraphCollapsed: boolean
  /** 任务状态面板是否折叠 */
  taskPanelCollapsed: boolean
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
  /** 切换执行图面板 */
  toggleExecutionGraph: () => void
  /** 设置执行图面板状态 */
  setExecutionGraphCollapsed: (collapsed: boolean) => void
  /** 切换任务状态面板 */
  toggleTaskPanel: () => void
  /** 设置任务状态面板状态 */
  setTaskPanelCollapsed: (collapsed: boolean) => void
  /** 初始化 UI 状态（从 localStorage 恢复） */
  initializeUI: () => void
}

/**
 * UI Store
 */
export const useUIStore = create<UIState & UIActions>((set) => ({
  sidebarCollapsed: false,
  approvalDialog: null,
  executionGraphCollapsed: false,
  taskPanelCollapsed: false,

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
   * 初始化 UI 状态（从 localStorage 恢复）
   */
  initializeUI: () => {
    try {
      const storedSidebarCollapsed = uiStorage.getSidebarCollapsed()
      if (storedSidebarCollapsed !== null) {
        set({ sidebarCollapsed: storedSidebarCollapsed })
      }
      const storedExecutionGraphCollapsed = uiStorage.getExecutionGraphCollapsed()
      if (storedExecutionGraphCollapsed !== null) {
        set({ executionGraphCollapsed: storedExecutionGraphCollapsed })
      }
      const storedTaskPanelCollapsed = uiStorage.getTaskPanelCollapsed()
      if (storedTaskPanelCollapsed !== null) {
        set({ taskPanelCollapsed: storedTaskPanelCollapsed })
      }
    } catch (error) {
      console.error('初始化 UI 状态失败:', error)
    }
  },
  /**
   * 切换执行图面板折叠状态
   */
  toggleExecutionGraph: () => {
    set((state) => {
      const newCollapsed = !state.executionGraphCollapsed
      uiStorage.setExecutionGraphCollapsed(newCollapsed)
      return { executionGraphCollapsed: newCollapsed }
    })
  },
  /**
   * 设置执行图面板状态
   */
  setExecutionGraphCollapsed: (collapsed: boolean) => {
    uiStorage.setExecutionGraphCollapsed(collapsed)
    set({ executionGraphCollapsed: collapsed })
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
}))
