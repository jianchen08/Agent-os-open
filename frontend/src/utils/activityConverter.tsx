/**
 * Activity 数据转换工具
 *
 * 将现有的 MessageToolCall、Task 数据转换为统一的 ActivityData 格式
 *
 * @module activityConverter
 */

import { Copy, RefreshCw } from 'lucide-react'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type {
  ActivityAction,
  ActivityData,
  ActivityDetailBlock,
  ActivityStatus,
  ActivityType,
} from '@/types/activity'
import type { MessageToolCall } from '@/types/models'
import type { ReactNode } from 'react'

/**
 * 转换选项
 */
export interface ConversionOptions {
  /** 自定义操作 */
  actions?: ActivityAction[]
  /** 自定义图标 */
  customIcon?: ReactNode
  /** 自定义颜色 */
  customColor?: string
  /** 是否包含详情 */
  includeDetails?: boolean
  /** 自定义样式类名 */
  customClassName?: string
  /** 打开文件回调 */
  onOpenFile?: (filePath: string, containerTaskId?: string) => void | Promise<void>
}

/**
 * 从 MessageToolCall 转换为 ActivityData
 */
export function toolCallToActivity(
  toolCall: MessageToolCall,
  options?: ConversionOptions,
): ActivityData {
  const details: ActivityDetailBlock[] = []

  if (options?.includeDetails !== false) {
    // 参数区块
    details.push({
      id: 'args',
      label: '参数',
      content: toolCall.tool_args,
      contentType: 'json',
      collapsible: true,
      defaultExpanded: false,
    })

    // 结果区块
    if (toolCall.result !== undefined && toolCall.result !== null) {
      details.push({
        id: 'result',
        label: '结果',
        content: toolCall.result as string | Record<string, unknown>,
        contentType: 'json',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    // 中间输出区块
    if (toolCall.partialOutput && toolCall.partialOutput.length > 0) {
      details.push({
        id: 'output',
        label: '执行输出',
        content: toolCall.partialOutput.join('\n'),
        contentType: 'text',
        collapsible: false,
      })
    }
  }

  // 默认操作
  const defaultActions: ActivityAction[] = [
    {
      id: 'copy_args',
      icon: <Copy className="h-3.5 w-3.5" />,
      label: '复制参数',
      type: 'copy',
      onClick: () => {
        navigator.clipboard.writeText(JSON.stringify(toolCall.tool_args, null, 2))
      },
    },
  ]

  if (toolCall.result !== undefined) {
    defaultActions.push({
      id: 'copy_result',
      icon: <Copy className="h-3.5 w-3.5" />,
      label: '复制结果',
      type: 'copy',
      onClick: () => {
        navigator.clipboard.writeText(
          typeof toolCall.result === 'string'
            ? toolCall.result
            : JSON.stringify(toolCall.result, null, 2),
        )
      },
    })
  }

  const base: ActivityData = {
    type: 'tool_call',
    id: toolCall.call_id,
    title: toolCall.tool_name,
    toolName: toolCall.tool_name,
    status: toolCall.status as ActivityStatus,
    durationMs: toolCall.duration_ms,
    progress: toolCall.progress,
    currentStep: toolCall.currentStep,
    partialOutput: toolCall.partialOutput,
    details,
    error: toolCall.error,
    timestamp: toolCall.started_at,
    actions: options?.actions || defaultActions,
    customIcon: options?.customIcon,
    customColor: options?.customColor,
    customClassName: options?.customClassName,
  }

  return enhanceActivityWithToolConfig(base, toolCall, {
    onOpenFile: options?.onOpenFile,
  })
}

/**
 * 从 Task 数据转换为 ActivityData
 */
export function taskToActivity(
  task: {
    id: string
    title: string
    status: ActivityStatus
    goal?: string
    phase?: string
    result?: unknown
    error?: string
    acceptanceCriteria?: Array<{
      id: string
      description: string
      status: string
    }>
  },
  messageType: 'task_created' | 'task_phase' | 'task_completed' | 'task_failed',
  options?: ConversionOptions,
): ActivityData {
  const details: ActivityDetailBlock[] = []

  if (options?.includeDetails !== false) {
    // 任务目标
    if (task.goal) {
      details.push({
        id: 'goal',
        label: '任务目标',
        content: task.goal,
        contentType: 'text',
        collapsible: false,
      })
    }

    // 当前阶段
    if (task.phase) {
      details.push({
        id: 'phase',
        label: '当前阶段',
        content: task.phase,
        contentType: 'text',
        collapsible: false,
      })
    }

    // 验收标准
    if (task.acceptanceCriteria && task.acceptanceCriteria.length > 0) {
      const passed = task.acceptanceCriteria.filter((ac) => ac.status === 'passed').length
      details.push({
        id: 'ac',
        label: `验收标准 (${passed}/${task.acceptanceCriteria.length} 通过)`,
        content: task.acceptanceCriteria
          .map((ac) => {
            const icon = ac.status === 'passed' ? '✓' : ac.status === 'failed' ? '✗' : '○'
            return `${icon} ${ac.description}`
          })
          .join('\n'),
        contentType: 'text',
        collapsible: true,
        defaultExpanded: false,
      })
    }

    // 执行结果
    if (task.result) {
      details.push({
        id: 'result',
        label: '执行结果',
        content: task.result as string | Record<string, unknown>,
        contentType: 'json',
        collapsible: true,
        defaultExpanded: false,
      })
    }
  }

  // 默认操作
  const defaultActions: ActivityAction[] = []

  if (task.status === 'failed') {
    defaultActions.push({
      id: 'retry',
      icon: <RefreshCw className="h-3.5 w-3.5" />,
      label: '重试',
      type: 'retry',
      onClick: () => {
        // 待实现：重试逻辑
      },
    })
  }

  return {
    type: messageType,
    id: task.id,
    title: task.title,
    status: task.status,
    details,
    error: task.error,
    actions: options?.actions || defaultActions,
    customIcon: options?.customIcon,
    customColor: options?.customColor,
    customClassName: options?.customClassName,
  }
}

/**
 * ActivityConverter 工具类
 *
 * 提供批量转换和高级转换功能
 */
export class ActivityConverter {
  /**
   * 从 MessageToolCall 转换
   */
  static fromToolCall(toolCall: MessageToolCall, options?: ConversionOptions): ActivityData {
    return toolCallToActivity(toolCall, options)
  }

  /**
   * 从 Task 数据转换
   */
  static fromTask(
    task: {
      id: string
      title: string
      status: ActivityStatus
      goal?: string
      phase?: string
      result?: unknown
      error?: string
      acceptanceCriteria?: Array<{
        id: string
        description: string
        status: string
      }>
    },
    messageType: 'task_created' | 'task_phase' | 'task_completed' | 'task_failed',
    options?: ConversionOptions,
  ): ActivityData {
    return taskToActivity(task, messageType, options)
  }

  /**
   * 批量转换工具调用列表
   */
  static batchConvertToolCalls(
    toolCalls: MessageToolCall[],
    options?: ConversionOptions,
  ): ActivityData[] {
    return toolCalls.map((tc) => this.fromToolCall(tc, options))
  }

  /**
   * 映射消息类型到活动类型
   */
  static mapMessageTypeToActivityType(messageType: string): ActivityType {
    switch (messageType) {
      case 'task_created':
        return 'task_created'
      case 'task_phase':
        return 'task_phase'
      case 'task_completed':
        return 'task_completed'
      case 'task_failed':
        return 'task_failed'
      default:
        return 'tool_call'
    }
  }

  /**
   * 映射消息类型到活动状态
   */
  static mapMessageTypeToStatus(messageType: string): ActivityStatus {
    switch (messageType) {
      case 'task_created':
        return 'pending'
      case 'task_phase':
        return 'running'
      case 'task_completed':
        return 'completed'
      case 'task_failed':
        return 'failed'
      default:
        return 'pending'
    }
  }

  /**
   * 创建自定义操作
   */
  static createAction(
    id: string,
    label: string,
    type: ActivityAction['type'],
    onClick: () => void | Promise<void>,
    options?: Partial<ActivityAction>,
  ): ActivityAction {
    return {
      id,
      label,
      type,
      onClick,
      disabled: options?.disabled,
      confirmMessage: options?.confirmMessage,
      variant: options?.variant,
      icon: options?.icon,
    }
  }
}

/**
 * 导出所有工具函数和类
 */
export default ActivityConverter
