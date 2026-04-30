/**
 * 长期任务（Project）状态管理 Store
 *
 * 负责管理长期任务的状态，包括：
 * - 长期任务列表的获取和更新
 * - 创建长期任务
 * - 切换自动完成开关
 * - 暂停/恢复长期任务
 * - WebSocket 事件订阅
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { API_ENDPOINTS } from '@/constants/api'
import { apiClient } from '@/services/api/client'
import type {
  Project,
  CreateProjectRequest,
  GetProjectsResponse,
  ToggleAutoExecuteResponse,
  PauseProjectResponse,
  ResumeProjectResponse,
} from '@/types/task'

/**
 * Project Store 状态接口
 */
interface ProjectState {
  /** 长期任务列表 */
  projects: Project[]
  /** 当前活跃的长期任务 ID */
  activeProjectId: string | null
  /** 是否正在加载 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null
}

/**
 * Project Store 操作接口
 */
interface ProjectActions {
  /** 获取长期任务列表 */
  fetchProjects: () => Promise<void>
  /** 创建长期任务 */
  createProject: (goal: string, sessionId?: string) => Promise<Project>
  /** 切换自动完成开关 */
  toggleAutoExecute: (projectId: string, enabled: boolean) => Promise<void>
  /** 暂停长期任务 */
  pauseProject: (projectId: string) => Promise<void>
  /** 恢复长期任务 */
  resumeProject: (projectId: string) => Promise<void>
  /** 设置活跃项目 */
  setActiveProject: (projectId: string | null) => void
  /** 更新项目状态 */
  updateProject: (projectId: string, updates: Partial<Project>) => void
  /** 删除项目 */
  deleteProject: (projectId: string) => void
  /** 清除错误 */
  clearError: () => void
}

/**
 * Project Store
 *
 * 使用 Zustand 管理长期任务状态，支持持久化
 */
export const useProjectStore = create<ProjectState & ProjectActions>()(
  persist(
    (set, get) => ({
      // 初始状态
      projects: [],
      activeProjectId: null,
      isLoading: false,
      error: null,

      /**
       * 获取长期任务列表
       *
       * 调用后端 API 获取所有长期任务
       */
      fetchProjects: async () => {
        const state = get()
        if (state.isLoading) {
          return
        }

        set({ isLoading: true, error: null })

        try {
          const response = await apiClient.get<GetProjectsResponse>(API_ENDPOINTS.PROJECTS.LIST)

          // 后端返回的是 { items: [...], total, limit, offset }
          // 需要从 items 中提取项目列表
          set({
            projects: response.data.items || [],
            isLoading: false,
          })
        } catch (error: unknown) {
          const errorMessage =
            (error instanceof Error ? error.message : null) || '获取长期任务列表失败'
          set({
            isLoading: false,
            error: errorMessage,
            projects: [], // 出错时也设置为空数组
          })
          throw new Error(errorMessage)
        }
      },

      /**
       * 创建长期任务
       *
       * @param goal 长期目标
       * @param sessionId 关联会话 ID（可选）
       * @returns 创建的长期任务
       */
      createProject: async (goal: string, sessionId?: string) => {
        set({ isLoading: true, error: null })

        try {
          const request: CreateProjectRequest = {
            goal,
            sessionId,
            autoExecute: false,
          }

          const response = await apiClient.post<{ project: Project }>(
            API_ENDPOINTS.PROJECTS.CREATE,
            request,
          )

          const newProject = response.data.project

          set((state) => ({
            projects: [...state.projects, newProject],
            activeProjectId: newProject.id,
            isLoading: false,
          }))

          return newProject
        } catch (error: unknown) {
          const errorMessage = (error instanceof Error ? error.message : null) || '创建长期任务失败'
          set({
            isLoading: false,
            error: errorMessage,
          })
          throw new Error(errorMessage)
        }
      },

      /**
       * 切换自动完成开关
       *
       * @param projectId 项目 ID
       * @param enabled 是否启用自动执行
       */
      toggleAutoExecute: async (projectId: string, enabled: boolean) => {
        set({ error: null })

        try {
          const response = await apiClient.patch<ToggleAutoExecuteResponse>(
            API_ENDPOINTS.PROJECTS.TOGGLE_AUTO_EXECUTE(projectId),
            { enabled },
          )

          const updatedProject = response.data.project

          set((state) => ({
            projects: state.projects.map((project) =>
              project.id === projectId ? updatedProject : project,
            ),
          }))
        } catch (error: unknown) {
          const errorMessage = (error instanceof Error ? error.message : null) || '切换自动执行失败'
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /**
       * 暂停长期任务
       *
       * @param projectId 项目 ID
       */
      pauseProject: async (projectId: string) => {
        set({ error: null })

        try {
          const response = await apiClient.post<PauseProjectResponse>(
            API_ENDPOINTS.PROJECTS.PAUSE(projectId),
          )

          const updatedProject = response.data.project

          set((state) => ({
            projects: state.projects.map((project) =>
              project.id === projectId ? updatedProject : project,
            ),
          }))
        } catch (error: unknown) {
          const errorMessage = (error instanceof Error ? error.message : null) || '暂停长期任务失败'
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /**
       * 恢复长期任务
       *
       * @param projectId 项目 ID
       */
      resumeProject: async (projectId: string) => {
        set({ error: null })

        try {
          const response = await apiClient.post<ResumeProjectResponse>(
            API_ENDPOINTS.PROJECTS.RESUME(projectId),
          )

          const updatedProject = response.data.project

          set((state) => ({
            projects: state.projects.map((project) =>
              project.id === projectId ? updatedProject : project,
            ),
          }))
        } catch (error: unknown) {
          const errorMessage = (error instanceof Error ? error.message : null) || '恢复长期任务失败'
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /**
       * 设置活跃项目
       *
       * @param projectId 项目 ID，null 表示取消活跃状态
       */
      setActiveProject: (projectId: string | null) => {
        set({ activeProjectId: projectId })
      },

      /**
       * 更新项目状态（用于 WebSocket 事件更新）
       *
       * @param projectId 项目 ID
       * @param updates 更新内容
       */
      updateProject: (projectId: string, updates: Partial<Project>) => {
        set((state) => ({
          projects: state.projects.map((project) =>
            project.id === projectId ? { ...project, ...updates } : project,
          ),
        }))
      },

      /**
       * 删除项目
       *
       * @param projectId 项目 ID
       */
      deleteProject: (projectId: string) => {
        set((state) => ({
          projects: state.projects.filter((project) => project.id !== projectId),
          activeProjectId: state.activeProjectId === projectId ? null : state.activeProjectId,
        }))
      },

      /**
       * 清除错误信息
       */
      clearError: () => {
        set({ error: null })
      },
    }),
    {
      name: 'project-storage',
      // 只持久化项目和活跃项目 ID，不持久化加载状态和错误
      partialize: (state) => ({
        projects: state.projects,
        activeProjectId: state.activeProjectId,
      }),
    },
  ),
)
