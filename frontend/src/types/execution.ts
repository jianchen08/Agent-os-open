/**
 * 统一执行卡片类型定义
 *
 * 用于统一工具/Agent/工作流的执行卡片渲染
 *
 * @module execution
 */

import type { ActivityData, ActivityDetailBlock } from './activity'

/**
 * 执行类型枚举
 */
export type ExecutionType = 'tool' | 'agent' | 'workflow'

/**
 * 执行状态枚举
 */
export type ExecutionStatus =
  | 'pending' // 等待中
  | 'running' // 执行中
  | 'completed' // 已完成
  | 'failed' // 失败
  | 'cancelled' // 已取消

/**
 * 执行开始事件数据
 */
export interface ExecutionStartEvent {
  type: 'execution_start'
  executionId: string
  executionType: ExecutionType
  name: string
  description?: string
  parentId?: string
  input?: Record<string, unknown>
  metadata?: Record<string, unknown>
  timestamp: string
}

/**
 * 执行进度事件数据
 */
export interface ExecutionProgressEvent {
  type: 'execution_progress'
  executionId: string
  progress: number // 0-100
  currentStep?: string
  message?: string
  timestamp: string
}

/**
 * 执行完成事件数据
 */
export interface ExecutionDoneEvent {
  type: 'execution_done'
  executionId: string
  success: boolean
  output?: Record<string, unknown>
  error?: string
  durationMs?: number
  summary?: string
  timestamp: string
}

/**
 * 执行取消事件数据
 */
export interface ExecutionCancelledEvent {
  type: 'execution_cancelled'
  executionId: string
  reason: string
  cancelledBy?: 'user' | 'system' | 'timeout'
  timestamp: string
}

/**
 * 思考开始事件数据
 */
export interface ThinkingStartEvent {
  type: 'thinking_start'
  executionId: string
  model?: string
  timestamp: string
}

/**
 * 思考内容片段事件数据
 */
export interface ThinkingChunkEvent {
  type: 'thinking_chunk'
  executionId: string
  chunk: string
  timestamp: string
}

/**
 * 思考结束事件数据
 */
export interface ThinkingEndEvent {
  type: 'thinking_end'
  executionId: string
  durationMs?: number
  timestamp: string
}

/**
 * 审批请求事件数据
 */
export interface ApprovalRequestedEvent {
  type: 'approval_requested'
  executionId: string
  operation: string
  riskLevel: number // 1-10
  description: string
  options?: Record<string, unknown>
  timeout?: number // 秒
  timestamp: string
}

/**
 * 成本预警事件数据
 */
export interface CostWarningEvent {
  type: 'cost_warning'
  executionId: string
  currentCost: number
  threshold: number
  message: string
  timestamp: string
}

/**
 * 资源限制事件数据
 */
export interface ResourceLimitEvent {
  type: 'resource_limit'
  executionId: string
  limitType: 'iterations' | 'time' | 'tokens'
  current: number
  limit: number
  message: string
  timestamp: string
}

/**
 * 执行事件联合类型
 */
export type ExecutionEvent =
  | ExecutionStartEvent
  | ExecutionProgressEvent
  | ExecutionDoneEvent
  | ExecutionCancelledEvent

/**
 * 思考事件联合类型
 */
export type ThinkingEvent = ThinkingStartEvent | ThinkingChunkEvent | ThinkingEndEvent

/**
 * 系统警告事件联合类型
 */
export type SystemWarningEvent = CostWarningEvent | ResourceLimitEvent

/**
 * 所有 WebSocket 事件联合类型
 */
export type WebSocketEvent =
  | ExecutionEvent
  | ThinkingEvent
  | ApprovalRequestedEvent
  | SystemWarningEvent

/**
 * 统一执行卡片数据
 *
 * 合并 start 和 done 事件后的完整数据结构
 */
export interface ExecutionCardData {
  /** 执行 ID */
  id: string
  /** 执行类型 */
  executionType: ExecutionType
  /** 名称 */
  name: string
  /** 描述 */
  description?: string
  /** 父执行 ID（嵌套时使用） */
  parentId?: string
  /** 执行状态 */
  status: ExecutionStatus
  /** 进度百分比 (0-100) */
  progress?: number
  /** 当前步骤描述 */
  currentStep?: string
  /** 输入参数 */
  input?: Record<string, unknown>
  /** 输出结果 */
  output?: Record<string, unknown>
  /** 错误信息 */
  error?: string
  /** 取消原因 */
  cancelReason?: string
  /** 取消者 */
  cancelledBy?: string
  /** 耗时（毫秒） */
  durationMs?: number
  /** 执行摘要 */
  summary?: string
  /** 元数据 */
  metadata?: Record<string, unknown>
  /** 开始时间戳 */
  startTime?: string
  /** 结束时间戳 */
  endTime?: string
}

/**
 * 执行类型文本映射
 */
export const EXECUTION_TYPE_TEXT_MAP: Record<ExecutionType, string> = {
  tool: '工具',
  agent: 'Agent',
  workflow: '工作流',
}

/**
 * 执行类型图标映射（使用 Lucide 图标名称）
 */
export const EXECUTION_TYPE_ICON_MAP: Record<ExecutionType, string> = {
  tool: 'Wrench',
  agent: 'Bot',
  workflow: 'GitBranch',
}

/**
 * 执行状态文本映射
 */
export const EXECUTION_STATUS_TEXT_MAP: Record<ExecutionStatus, string> = {
  pending: '等待中',
  running: '执行中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

/**
 * 从执行事件创建/更新卡片数据
 */
export function mergeExecutionEvent(
  existing: ExecutionCardData | undefined,
  event: ExecutionEvent,
): ExecutionCardData {
  switch (event.type) {
    case 'execution_start':
      return {
        id: event.executionId,
        executionType: event.executionType,
        name: event.name,
        description: event.description,
        parentId: event.parentId,
        status: 'running',
        input: event.input,
        metadata: event.metadata,
        startTime: event.timestamp,
      }

    case 'execution_progress':
      if (!existing) {
        // 如果没有 start 事件，创建一个基础卡片
        return {
          id: event.executionId,
          executionType: 'tool', // 默认类型
          name: '执行中',
          status: 'running',
          progress: event.progress,
          currentStep: event.currentStep,
        }
      }
      return {
        ...existing,
        progress: event.progress,
        currentStep: event.currentStep,
      }

    case 'execution_done':
      if (!existing) {
        // 如果没有 start 事件，创建一个完成的卡片
        return {
          id: event.executionId,
          executionType: 'tool', // 默认类型
          name: event.summary || '执行完成',
          status: event.success ? 'completed' : 'failed',
          output: event.output,
          error: event.error,
          durationMs: event.durationMs,
          summary: event.summary,
          endTime: event.timestamp,
        }
      }
      return {
        ...existing,
        status: event.success ? 'completed' : 'failed',
        output: event.output,
        error: event.error,
        durationMs: event.durationMs,
        summary: event.summary,
        endTime: event.timestamp,
        progress: event.success ? 100 : existing.progress,
      }

    case 'execution_cancelled':
      if (!existing) {
        // 如果没有 start 事件，创建一个取消的卡片
        return {
          id: event.executionId,
          executionType: 'tool', // 默认类型
          name: '已取消',
          status: 'cancelled',
          cancelReason: event.reason,
          cancelledBy: event.cancelledBy,
          endTime: event.timestamp,
        }
      }
      return {
        ...existing,
        status: 'cancelled',
        cancelReason: event.reason,
        cancelledBy: event.cancelledBy,
        endTime: event.timestamp,
      }
  }
}

/**
 * 将 ExecutionCardData 转换为 ActivityData
 *
 * 用于与现有 ActivityCard 组件兼容
 */
export function toActivityData(execution: ExecutionCardData): ActivityData {
  return {
    type: 'custom',
    id: execution.id,
    title: execution.name,
    status:
      execution.status === 'completed'
        ? 'completed'
        : execution.status === 'failed'
          ? 'failed'
          : execution.status === 'cancelled'
            ? 'cancelled'
            : execution.status === 'running'
              ? 'running'
              : 'pending',
    statusText: EXECUTION_STATUS_TEXT_MAP[execution.status],
    durationMs: execution.durationMs,
    progress: execution.progress,
    currentStep: execution.currentStep,
    error: execution.error || execution.cancelReason,
    timestamp: execution.startTime,
    details: buildExecutionDetails(execution),
  }
}

/**
 * 构建执行详情区块
 */
function buildExecutionDetails(
  execution: ExecutionCardData,
): ActivityDetailBlock[] {
  const details: ActivityDetailBlock[] = []

  // 添加描述
  if (execution.description) {
    details.push({
      label: '描述',
      content: execution.description,
      contentType: 'text',
    })
  }

  // 添加输入参数
  if (execution.input && Object.keys(execution.input).length > 0) {
    details.push({
      label: '输入参数',
      content: execution.input,
      contentType: 'json',
      collapsible: true,
      defaultExpanded: false,
    })
  }

  // 添加输出结果
  if (execution.output && Object.keys(execution.output).length > 0) {
    details.push({
      label: '输出结果',
      content: execution.output,
      contentType: 'json',
      collapsible: true,
      defaultExpanded: true,
    })
  }

  // 添加执行摘要
  if (execution.summary) {
    details.push({
      label: '执行摘要',
      content: execution.summary,
      contentType: 'text',
    })
  }

  // 添加错误信息
  if (execution.error) {
    details.push({
      label: '错误信息',
      content: execution.error,
      contentType: 'text',
    })
  }

  return details
}
