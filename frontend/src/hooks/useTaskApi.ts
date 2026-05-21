/**
 * 任务管理 API Hooks
 *
 * 提供完整的任务管理 React Hooks：
 * - 长期任务 Hooks
 * - 任务阶段 Hooks
 * - AC 评估 Hooks
 *
 * 使用 useState 和 useEffect 实现数据获取和状态管理
 * 包含加载状态、错误处理和数据缓存
 */

import { useState, useEffect, useCallback, useRef } from 'react'
import * as taskApi from '@/services/api/tasks'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import type {
  // 长期任务类型
  Project,
  ProjectStatus,
  // 短期任务类型
  TaskPhase,
  // 验收标准类型
  AcceptanceCriterion,
  // API 响应类型
  GetProjectsResponse,
  GetTaskPhaseResponse,
  GetTaskACsResponse,
  GetPhaseOutputResponse,
} from '@/types/task'

// ============================================================================
// 通用类型
// ============================================================================

/**
 * API 请求状态
 */
interface ApiRequestState<T> {
  /** 数据 */
  data: T | null
  /** 是否正在加载 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null
  /** 是否正在执行操作 */
  isMutating: boolean
}

// ============================================================================
// 长期任务 Hooks
// ============================================================================

/**
 * 获取长期任务列表 Hook
 *
 * @param params 查询参数
 * @returns 长期任务列表和状态
 */
export function useProjects(params?: { page?: number; limit?: number; status?: ProjectStatus }) {
  const [state, setState] = useState<ApiRequestState<GetProjectsResponse>>({
    data: null,
    isLoading: true,
    error: null,
    isMutating: false,
  })

  // 使用 ref 存储参数，避免依赖项变化导致无限循环
  const paramsRef = useRef(params)

  // 更新参数 ref
  useEffect(() => {
    paramsRef.current = params
  }, [params])

  /**
   * 获取长期任务列表
   */
  const fetch = useCallback(async () => {
    setState((prev) => ({ ...prev, isLoading: true, error: null }))

    try {
      const data = await taskApi.fetchProjects(paramsRef.current)
      setState({ data, isLoading: false, error: null, isMutating: false })
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取长期任务列表失败'
      setState((prev) => ({ ...prev, isLoading: false, error: errorMessage }))
    }
  }, [])

  /**
   * 刷新数据
   */
  const refetch = useCallback(() => {
    fetch()
  }, [fetch])

  // 初始加载
  useEffect(() => {
    fetch()
  }, [fetch])

  return {
    ...state,
    refetch,
  }
}

/**
 * 获取单个长期任务详情 Hook
 *
 * @param projectId 长期任务 ID
 * @returns 长期任务详情和状态
 */
export function useProject(projectId: string) {
  const [state, setState] = useState<ApiRequestState<Project>>({
    data: null,
    isLoading: false,
    error: null,
    isMutating: false,
  })

  /**
   * 获取长期任务详情
   */
  const fetch = useCallback(async () => {
    if (!projectId) {
      setState((prev) => ({
        ...prev,
        isLoading: false,
        error: '项目 ID 不能为空',
      }))
      return
    }

    setState((prev) => ({ ...prev, isLoading: true, error: null }))

    try {
      const data = await taskApi.fetchProject(projectId)
      setState({ data, isLoading: false, error: null, isMutating: false })
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取长期任务详情失败'
      setState((prev) => ({ ...prev, isLoading: false, error: errorMessage }))
    }
  }, [projectId])

  /**
   * 刷新数据
   */
  const refetch = useCallback(() => {
    fetch()
  }, [fetch])

  // 初始加载
  useEffect(() => {
    fetch()
  }, [fetch])

  return {
    ...state,
    refetch,
  }
}

/**
 * 切换长期任务自动执行开关 Hook
 *
 * @returns 切换函数和状态
 */
export function useToggleProjectAutoExecute() {
  const [isMutating, setIsMutating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 切换自动执行开关
   */
  const toggleAutoExecute = useCallback(
    async (projectId: string, enabled: boolean): Promise<Project> => {
      setIsMutating(true)
      setError(null)

      try {
        const project = await taskApi.toggleProjectAutoExecute(projectId, enabled)
        setIsMutating(false)
        return project
      } catch (error: unknown) {
        const errorMessage = error instanceof Error ? error.message : '切换自动执行失败'
        setError(errorMessage)
        setIsMutating(false)
        throw new Error(errorMessage)
      }
    },
    [],
  )

  return {
    toggleAutoExecute,
    isMutating,
    error,
  }
}

/**
 * 暂停长期任务 Hook
 *
 * @returns 暂停函数和状态
 */
export function usePauseProject() {
  const [isMutating, setIsMutating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 暂停长期任务
   */
  const pauseProject = useCallback(async (projectId: string): Promise<Project> => {
    setIsMutating(true)
    setError(null)

    try {
      const project = await taskApi.pauseProject(projectId)
      setIsMutating(false)
      return project
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '暂停长期任务失败'
      setError(errorMessage)
      setIsMutating(false)
      throw new Error(errorMessage)
    }
  }, [])

  return {
    pauseProject,
    isMutating,
    error,
  }
}

/**
 * 恢复长期任务 Hook
 *
 * @returns 恢复函数和状态
 */
export function useResumeProject() {
  const [isMutating, setIsMutating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 恢复长期任务
   */
  const resumeProject = useCallback(async (projectId: string): Promise<Project> => {
    setIsMutating(true)
    setError(null)

    try {
      const project = await taskApi.resumeProject(projectId)
      setIsMutating(false)
      return project
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '恢复长期任务失败'
      setError(errorMessage)
      setIsMutating(false)
      throw new Error(errorMessage)
    }
  }, [])

  return {
    resumeProject,
    isMutating,
    error,
  }
}

/**
 * 删除长期任务 Hook
 *
 * @returns 删除函数和状态
 */
export function useDeleteProject() {
  const [isMutating, setIsMutating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 删除长期任务
   */
  const deleteProject = useCallback(async (projectId: string): Promise<void> => {
    setIsMutating(true)
    setError(null)

    try {
      await taskApi.deleteProject(projectId)
      setIsMutating(false)
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '删除长期任务失败'
      setError(errorMessage)
      setIsMutating(false)
      throw new Error(errorMessage)
    }
  }, [])

  return {
    deleteProject,
    isMutating,
    error,
  }
}

// ============================================================================
// 任务阶段 Hooks
// ============================================================================

/**
 * 获取任务阶段状态 Hook
 *
 * @param taskId 任务 ID
 * @param refreshInterval 刷新间隔（毫秒，默认 5000ms）
 * @returns 任务阶段状态和数据
 */
export function useTaskPhase(taskId: string, refreshInterval: number = 5000) {
  const [state, setState] = useState<ApiRequestState<GetTaskPhaseResponse>>({
    data: null,
    isLoading: false,
    error: null,
    isMutating: false,
  })

  /**
   * 获取任务阶段状态
   */
  const fetch = useCallback(async () => {
    if (!taskId) {
      return
    }

    setState((prev) => ({ ...prev, isLoading: true, error: null }))

    try {
      const data = await taskApi.fetchTaskPhase(taskId)
      setState((prev) => ({ ...prev, data, isLoading: false, error: null }))
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取任务阶段状态失败'
      setState((prev) => ({ ...prev, isLoading: false, error: errorMessage }))
    }
  }, [taskId])

  /**
   * 刷新数据
   */
  const refetch = useCallback(() => {
    fetch()
  }, [fetch])

  // 初始加载
  useEffect(() => {
    fetch()
  }, [fetch])

  // 监听 longTermTaskStore 中任务状态变化，事件驱动刷新
  const taskStatus = useLongTermTaskStore((s) => {
    const task = s.tasks.find((t: any) => t.id === taskId)
    return task?.status
  })

  useEffect(() => {
    if (taskStatus) {
      fetch()
    }
  }, [taskStatus, fetch])

  return {
    ...state,
    refetch,
  }
}

/**
 * 获取阶段产物 Hook
 *
 * @param taskId 任务 ID
 * @param phase 阶段名称
 * @returns 阶段产物和状态
 */
export function usePhaseOutput(taskId: string, phase: TaskPhase) {
  const [state, setState] = useState<ApiRequestState<GetPhaseOutputResponse>>({
    data: null,
    isLoading: false,
    error: null,
    isMutating: false,
  })

  /**
   * 获取阶段产物
   */
  const fetch = useCallback(async () => {
    if (!taskId || !phase) {
      return
    }

    setState((prev) => ({ ...prev, isLoading: true, error: null }))

    try {
      const data = await taskApi.fetchPhaseOutput(taskId, phase)
      setState({ data, isLoading: false, error: null, isMutating: false })
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取阶段产物失败'
      setState((prev) => ({ ...prev, isLoading: false, error: errorMessage }))
    }
  }, [taskId, phase])

  /**
   * 刷新数据
   */
  const refetch = useCallback(() => {
    fetch()
  }, [fetch])

  // 初始加载
  useEffect(() => {
    fetch()
  }, [fetch])

  return {
    ...state,
    refetch,
  }
}

// ============================================================================
// AC 评估 Hooks
// ============================================================================

/**
 * 获取任务验收标准列表 Hook
 *
 * @param taskId 任务 ID
 * @param refreshInterval 刷新间隔（毫秒，默认 3000ms）
 * @returns 验收标准列表和状态
 */
export function useTaskACs(taskId: string, refreshInterval: number = 3000) {
  const [state, setState] = useState<ApiRequestState<GetTaskACsResponse>>({
    data: null,
    isLoading: false,
    error: null,
    isMutating: false,
  })

  /**
   * 获取验收标准列表
   */
  const fetch = useCallback(async () => {
    if (!taskId) {
      return
    }

    setState((prev) => ({ ...prev, isLoading: true, error: null }))

    try {
      const data = await taskApi.fetchTaskACs(taskId)
      setState((prev) => ({ ...prev, data, isLoading: false, error: null }))
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取验收标准列表失败'
      setState((prev) => ({ ...prev, isLoading: false, error: errorMessage }))
    }
  }, [taskId])

  /**
   * 刷新数据
   */
  const refetch = useCallback(() => {
    fetch()
  }, [fetch])

  // 初始加载
  useEffect(() => {
    fetch()
  }, [fetch])

  // 监听 longTermTaskStore 中任务状态变化，事件驱动刷新
  const taskStatus = useLongTermTaskStore((s) => {
    const task = s.tasks.find((t: any) => t.id === taskId)
    return task?.status
  })

  useEffect(() => {
    if (taskStatus) {
      fetch()
    }
  }, [taskStatus, fetch])

  return {
    ...state,
    refetch,
  }
}

/**
 * 获取验收标准评估结果 Hook
 *
 * @param taskId 任务 ID
 * @param acId 验收标准 ID
 * @returns 验收标准评估结果和状态
 */
export function useACResult(taskId: string, acId: string) {
  const [state, setState] = useState<ApiRequestState<AcceptanceCriterion>>({
    data: null,
    isLoading: false,
    error: null,
    isMutating: false,
  })

  /**
   * 获取验收标准评估结果
   */
  const fetch = useCallback(async () => {
    if (!taskId || !acId) {
      return
    }

    setState((prev) => ({ ...prev, isLoading: true, error: null }))

    try {
      const data = await taskApi.fetchACResult(taskId, acId)
      setState({ data, isLoading: false, error: null, isMutating: false })
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取验收标准评估结果失败'
      setState((prev) => ({ ...prev, isLoading: false, error: errorMessage }))
    }
  }, [taskId, acId])

  /**
   * 刷新数据
   */
  const refetch = useCallback(() => {
    fetch()
  }, [fetch])

  // 初始加载
  useEffect(() => {
    fetch()
  }, [fetch])

  return {
    ...state,
    refetch,
  }
}
