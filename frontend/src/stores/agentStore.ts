import { create } from 'zustand'
import { ErrorType, reportError } from '@/services/errorReporting'
import { tokenManager } from './tokenManager'
import type { Agent } from '@/types/models'

/**
 * Agent Store 状态管理
 */
interface AgentState {
  /** Agent 列表 */
  agents: Agent[]
  /** 当前选择的 Agent ID（null 表示使用默认助手） */
  currentAgentId: string | null
  /** 加载状态 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null

  /** 获取 Agent 列表 */
  fetchAgents: () => Promise<void>
  /** 获取默认 Agent */
  fetchDefaultAgent: () => Promise<string | null>
  /** 设置当前 Agent */
  setCurrentAgentId: (agentId: string | null) => void
  /** 清除错误 */
  clearError: () => void
}

/**
 * API 基础 URL
 */
const API_BASE = import.meta.env.VITE_API_BASE_URL
  ? `${import.meta.env.VITE_API_BASE_URL}/api/v1`
  : '/api/v1'

export const useAgentStore = create<AgentState>((set) => ({
  agents: [],
  currentAgentId: null,
  isLoading: false,
  error: null,

  /**
   * 获取 Agent 列表
   */
  fetchAgents: async () => {
    // 防止重复请求
    const state = useAgentStore.getState()
    if (state.isLoading) {
      return
    }

    set({ isLoading: true, error: null })
    try {
      // 使用 tokenManager 获取 token
      const token = tokenManager.getToken()

      // 检查 token 是否存在
      if (!token) {
        const errorMsg = '未找到认证令牌，请先登录'
        set({ isLoading: false, error: errorMsg })
        reportError(errorMsg, ErrorType.AUTHENTICATION, undefined, {
          componentName: 'AgentStore',
          operation: 'fetchAgents',
          reason: 'no_token',
        })
        throw new Error(errorMsg)
      }

      const response = await fetch(`${API_BASE}/agents?agent_type=main`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      })

      if (!response.ok) {
        // 尝试解析错误响应
        let errorDetail = '获取 Agent 列表失败'
        try {
          const errorData = await response.json()
          errorDetail = errorData.detail || errorData.message || errorDetail
        } catch {
          // 无法解析 JSON，使用默认错误信息
        }

        const errorMsg = `${errorDetail} (HTTP ${response.status})`
        set({ isLoading: false, error: errorMsg })

        // 根据状态码确定错误类型
        let errorType: ErrorType = ErrorType.SERVER
        if (response.status === 401) {
          errorType = ErrorType.AUTHENTICATION as ErrorType
        } else if (response.status === 403) {
          errorType = ErrorType.AUTHORIZATION as ErrorType
        } else if (response.status === 404) {
          errorType = ErrorType.NOT_FOUND as ErrorType
        } else if (response.status >= 500) {
          errorType = ErrorType.SERVER
        }

        reportError(errorMsg, errorType, undefined, {
          componentName: 'AgentStore',
          operation: 'fetchAgents',
          status: response.status,
          statusText: response.statusText,
        })

        throw new Error(errorMsg)
      }

      const data = await response.json()

      // 后端 AgentListResponse 使用 items 字段
      const rawAgents = data.items || []
      const mappedAgents = rawAgents.map((agent: Record<string, unknown>) => ({
        id: agent.id || agent.config_id,
        configId: agent.config_id,
        name: agent.name,
        description: agent.description || '',
        type: agent.agent_type || 'atomic',
        status: agent.status || 'active',
        model: agent.model,
        config: {
          model: agent.model,
          system_prompt: agent.system_prompt,
          tool_names: agent.tool_names,
          max_iterations: agent.max_iterations,
          timeout: agent.timeout,
        },
        createdAt: agent.created_at,
        updatedAt: agent.updated_at,
      }))

      set({
        agents: mappedAgents,
        isLoading: false,
      })
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '获取 Agent 列表失败'
      set({ isLoading: false, error: errorMessage })
      throw error
    }
  },

  /**
   * 获取默认 Agent
   */
  fetchDefaultAgent: async () => {
    try {
      // 使用 tokenManager 获取 token
      const token = tokenManager.getToken()
      const response = await fetch(`${API_BASE}/agents/default`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      })

      if (!response.ok) {
        // 如果没有默认 Agent，返回 null
        if (response.status === 404) {
          return null
        }
        throw new Error('获取默认 Agent 失败')
      }

      const data = await response.json()
      const defaultAgentId = data.id

      // 同时更新当前 Agent ID
      set({ currentAgentId: defaultAgentId })

      return defaultAgentId
    } catch (error) {
      reportError(
        error instanceof Error ? error.message : String(error),
        ErrorType.SERVER,
        undefined,
        {
          componentName: 'AgentStore',
          operation: 'fetchDefaultAgent',
        },
      )
      return null
    }
  },

  /**
   * 设置当前 Agent
   */
  setCurrentAgentId: (agentId: string | null) => {
    set({ currentAgentId: agentId })
  },

  /**
   * 清除错误
   */
  clearError: () => {
    set({ error: null })
  },
}))
