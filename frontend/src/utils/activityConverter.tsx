/**
 * Activity 数据转换工具
 *
 * 将现有的 MessageToolCall 数据转换为统一的 ActivityData 格式
 *
 * @module activityConverter
 */

import { Copy } from '@/assets/icons'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type {
  ActivityAction,
  ActivityData,
  ActivityDetailBlock,
  ActivityStatus,
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
 * 构建默认的操作按钮（复制参数/复制结果）
 *
 * toolCallToActivity 与 useMessageRender 共用（原 useMessageRender 内
 * 复制实现已收敛至此，icon 统一用设计系统 icon-sm 令牌）。
 */
export function buildDefaultActions(toolCall: MessageToolCall): ActivityAction[] {
  const actions: ActivityAction[] = [
    {
      id: 'copy_args',
      icon: <Copy className="h-icon-sm w-icon-sm" />,
      label: '复制参数',
      type: 'copy',
      onClick: () => {
        navigator.clipboard.writeText(JSON.stringify(toolCall.tool_args, null, 2))
      },
    },
  ]

  if (toolCall.result !== undefined) {
    actions.push({
      id: 'copy_result',
      icon: <Copy className="h-icon-sm w-icon-sm" />,
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

  return actions
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
    actions: options?.actions || buildDefaultActions(toolCall),
    customIcon: options?.customIcon,
    customColor: options?.customColor,
    customClassName: options?.customClassName,
  }

  return enhanceActivityWithToolConfig(base, toolCall, {
    onOpenFile: options?.onOpenFile,
  })
}
