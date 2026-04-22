/**
 * 执行控制 API 服务
 *
 * 提供任务执行的控制接口：暂停、恢复、取消、回滚等，与后端 /api/v1/execution/* 端点对齐
 *
 * 暴露接口：
 * - controlExecution(executionId, action, params, options): ExecutionResponse - 执行控制（通用接口）
 * - pauseExecution(executionId, options): ExecutionResponse - 暂停执行
 * - resumeExecution(executionId, options): ExecutionResponse - 恢复执行
 * - cancelExecution(executionId, options): ExecutionResponse - 取消执行
 * - rollbackExecution(executionId, stepId, options): ExecutionResponse - 回滚执行
 * - injectAgentMessage(executionId, data, options): ExecutionResponse - 注入 Agent 消息
 * - getExecutionStatus(executionId, options): ExecutionResponse - 获取执行状态
 * - getExecutionSteps(executionId, options): ExecutionStep[] - 获取执行步骤列表
 * - approveExecution(executionId, data, options): ExecutionResponse - 审批执行
 * - ExecutionStatus - 执行状态类型
 * - ExecutionResponse - 执行响应类型
 * - ExecutionStep - 执行步骤类型
 * - ExecutionAction - 执行控制动作类型
 * - ExecutionControlRequest - 执行控制请求类型
 * - ApprovalRequest - 审批请求类型
 * - InjectMessageRequest - 注入消息请求类型
 */

import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/../utils/retry'
import type { RetryOptions } from '@/../utils/retry'

/**
 * 执行状态类型
 */
export type ExecutionStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'waiting_approval'

/**
 * 执行响应类型
 */
export interface ExecutionResponse {
  /** 执行 ID */
  id: string
  /** Agent ID */
  agent_id?: string
  /** 工作流 ID */
  workflow_id?: string
  /** 用户意图 */
  intent: string
  /** 执行状态 */
  status: ExecutionStatus
  /** 执行结果 */
  result?: Record<string, unknown>
  /** 错误信息 */
  error?: string
  /** 创建时间 */
  created_at: string
  /** 开始时间 */
  started_at?: string
  /** 完成时间 */
  completed_at?: string
}

/**
 * 执行步骤类型
 */
export interface ExecutionStep {
  /** 步骤 ID */
  id: string
  /** 执行 ID */
  execution_id: string
  /** 步骤名称 */
  name: string
  /** 步骤类型 */
  type: string
  /** 步骤状态 */
  status: ExecutionStatus
  /** 输入数据 */
  input?: Record<string, unknown>
  /** 输出数据 */
  output?: Record<string, unknown>
  /** 错误信息 */
  error?: string
  /** 开始时间 */
  started_at?: string
  /** 完成时间 */
  completed_at?: string
}

/**
 * 执行控制动作类型
 */
export type ExecutionAction =
  | 'pause'
  | 'resume'
  | 'cancel'
  | 'retry'
  | 'rollback'

/**
 * 执行控制请求类型
 */
export interface ExecutionControlRequest {
  /** 控制动作 */
  action: ExecutionAction
  /** 附加参数 */
  params?: Record<string, unknown>
}

/**
 * 审批请求类型
 */
export interface ApprovalRequest {
  /** 审批动作 */
  action: 'approve' | 'reject' | 'modify'
  /** 审批意见 */
  comment?: string
  /** 修改内容 */
  modifications?: Record<string, unknown>
}

/**
 * 注入消息请求类型
 */
export interface InjectMessageRequest {
  /** 消息内容 */
  content: string
  /** 消息角色 */
  role?: 'user' | 'system'
  /** 元数据 */
  metadata?: Record<string, unknown>
}

/** API 端点 */
const EXECUTION_ENDPOINTS = {
  LIST: '/api/v1/execution',
  GET: (id: string) => `/api/v1/execution/${id}`,
  CONTROL: (id: string) => `/api/v1/execution/${id}/control`,
  CANCEL: (id: string) => `/api/v1/execution/${id}/cancel`,
  RETRY: (id: string) => `/api/v1/execution/${id}/retry`,
  APPROVE: (id: string) => `/api/v1/execution/${id}/approve`,
  STEPS: (id: string) => `/api/v1/execution/${id}/steps`,
  INJECT: (id: string) => `/api/v1/execution/${id}/inject`,
}

export async function controlExecution(
  executionId: string,
  action: ExecutionAction,
  params?: Record<string, unknown>,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  if (!executionId || executionId.trim().length === 0) {
    throw new Error('执行 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<ExecutionResponse>(
      EXECUTION_ENDPOINTS.CONTROL(executionId),
      { action, params }
    )
    return response.data
  }, options)
}

export async function pauseExecution(
  executionId: string,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  return controlExecution(executionId, 'pause', undefined, options)
}

export async function resumeExecution(
  executionId: string,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  return controlExecution(executionId, 'resume', undefined, options)
}

export async function cancelExecution(
  executionId: string,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  if (!executionId || executionId.trim().length === 0) {
    throw new Error('执行 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<ExecutionResponse>(
      EXECUTION_ENDPOINTS.CANCEL(executionId)
    )
    return response.data
  }, options)
}

export async function rollbackExecution(
  executionId: string,
  stepId?: string,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  return controlExecution(executionId, 'rollback', { step_id: stepId }, options)
}

export async function injectAgentMessage(
  executionId: string,
  data: InjectMessageRequest,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  if (!executionId || executionId.trim().length === 0) {
    throw new Error('执行 ID 不能为空')
  }
  if (!data.content || data.content.trim().length === 0) {
    throw new Error('消息内容不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<ExecutionResponse>(
      EXECUTION_ENDPOINTS.INJECT(executionId),
      data
    )
    return response.data
  }, options)
}

export async function getExecutionStatus(
  executionId: string,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  if (!executionId || executionId.trim().length === 0) {
    throw new Error('执行 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<ExecutionResponse>(
      EXECUTION_ENDPOINTS.GET(executionId)
    )
    return response.data
  }, options)
}

export async function getExecutionSteps(
  executionId: string,
  options: RetryOptions = {}
): Promise<ExecutionStep[]> {
  if (!executionId || executionId.trim().length === 0) {
    throw new Error('执行 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<{ steps: ExecutionStep[] }>(
      EXECUTION_ENDPOINTS.STEPS(executionId)
    )
    return response.data.steps || []
  }, options)
}

export async function approveExecution(
  executionId: string,
  data: ApprovalRequest,
  options: RetryOptions = {}
): Promise<ExecutionResponse> {
  if (!executionId || executionId.trim().length === 0) {
    throw new Error('执行 ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<ExecutionResponse>(
      EXECUTION_ENDPOINTS.APPROVE(executionId),
      data
    )
    return response.data
  }, options)
}
