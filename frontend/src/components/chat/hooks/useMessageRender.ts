/**
 * 消息渲染 Hook
 *
 * 统一处理消息的渲染上下文
 * 支持文本和工具调用片段的混合渲染
 *
 * 渲染路径：parts[]（唯一数据源，按 sequence 排序）
 */

import { useMemo } from 'react'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { Message, MessageToolCall, ThinkingContent } from '@/types/models'
import type { SystemLevel, ToolCallPart } from '@/types/messageParts'
/**
 * 渲染片段类型
 */
export type RenderFragment =
  | {
      type: 'thinking'
      thinking: ThinkingContent
      key: string
      sourceId: string
    }
  | {
      type: 'text'
      content: string
      key: string
      sourceId: string
      isLast: boolean
    }
  | {
      type: 'tool_call'
      toolCall: MessageToolCall
      activity: ActivityData
      key: string
      index: number
      total: number
    }
  | {
      type: 'system'
      content: string
      level: SystemLevel
      notificationType: string
      key: string
    }

/**
 * 渲染上下文
 */
export interface MessageRenderContext {
  /** 渲染片段列表 */
  fragments: RenderFragment[]
  /** 是否正在流式输出 */
  isStreaming: boolean
  /** 消息 ID */
  messageId: string
  /** 显示内容 */
  displayContent: string
}

/**
 * 从 ToolCallPart 构建 ActivityData（parts[] 路径专用）
 *
 * @param part - 工具调用 Part 数据
 * @param index - 在 parts 数组中的索引（用于生成 fallback ID）
 * @returns ActivityData 活动数据
 */
function buildActivityFromToolPart(part: ToolCallPart, index: number): ActivityData {
  return {
    type: 'tool_call',
    id: part.callId || `tool-${index}`,
    title: part.name,
    toolName: part.name,
    status:
      part.state === 'done'
        ? 'completed'
        : part.state === 'error'
          ? 'failed'
          : part.state === 'calling'
            ? 'running'
            : part.state === 'cancelled'
              ? 'cancelled'
              : 'pending',
    durationMs: part.durationMs,
    progress: part.progress,
    currentStep: part.currentStep,
    details: [],
    error: part.error,
    actions: [],
  }
}

/**
 * 从 Message.parts[] 构建渲染片段（优先路径）
 *
 * Traverses the parts array and converts each part type into the corresponding RenderFragment.
 * Supports text / thinking / tool_call / system types.
 *
 * @param message - 消息对象（必须包含 parts[]）
 * @returns RenderFragment[] 渲染片段列表
 */
function buildFragmentsFromParts(message: Message): RenderFragment[] {
  const fragments: RenderFragment[] = []
  const parts = message.parts!

  const sorted = [...parts].sort((a, b) => {
    return (a.sequence ?? 0) - (b.sequence ?? 0)
  })

  const toolCallCount = sorted.filter((p) => p.type === 'tool_call').length
  let toolCallIndex = 0

  for (let i = 0; i < sorted.length; i++) {
    const part = sorted[i]
    switch (part.type) {
      case 'text': {
        const textContent = part.content || (part as any).text || ''
        if (textContent && textContent.trim()) {
          fragments.push({
            type: 'text',
            content: textContent,
            key: `part-text-${i}`,
            sourceId: message.id,
            isLast: false,
          })
        }
        break
      }

      case 'thinking': {
        fragments.push({
          type: 'thinking',
          thinking: {
            content: part.content,
            isThinking: part.state === 'streaming',
            durationMs: part.durationMs,
            steps: part.steps,
          },
          key: `part-thinking-${i}`,
          sourceId: message.id,
        })
        break
      }

      case 'tool_call': {
        // 将 ToolCallPart 映射为 MessageToolCall 格式
        const toolCall: MessageToolCall = {
          call_id: part.callId,
          tool_name: part.name,
          tool_args: part.args,
          status:
            part.state === 'done'
              ? 'completed'
              : part.state === 'error'
                ? 'failed'
                : part.state === 'calling'
                  ? 'running'
                  : part.state === 'cancelled'
                    ? 'cancelled'
                    : 'pending',
          result: part.result,
          error: part.error,
          duration_ms: part.durationMs,
          progress: part.progress,
          currentStep: part.currentStep,
        }
        // 构建 ActivityData 并应用工具卡片注册表增强
        const activity = enhanceActivityWithToolConfig(
          buildActivityFromToolPart(part, i),
          toolCall,
        )
        fragments.push({
          type: 'tool_call',
          toolCall,
          activity,
          key: `part-tool-${part.callId}-${i}`,
          index: toolCallIndex,
          total: toolCallCount,
        })
        toolCallIndex++
        break
      }

      case 'system': {
        if (part.content && part.content.trim()) {
          fragments.push({
            type: 'system',
            content: part.content,
            level: part.level,
            notificationType: part.notificationType,
            key: `part-system-${i}`,
          })
        }
        break
      }
    }
  }

  // 标记最后一个 text fragment 的 isLast
  const lastTextIdx = fragments.reduce(
    (acc, f, i) => (f.type === 'text' ? i : acc),
    -1,
  )
  if (lastTextIdx >= 0) {
    const last = fragments[lastTextIdx]
    if (last.type === 'text') {
      fragments[lastTextIdx] = { ...last, isLast: true }
    }
  }

  return fragments
}

/**
 * Hook 选项
 */
export interface UseMessageRenderOptions {
  /** 消息数据 */
  message: Message
  /** 是否为最后一条消息 */
  isLast?: boolean
  /** 是否正在生成 */
  isGenerating?: boolean
  /** 版本内容（编辑时使用） */
  versionContent?: string | null
}

/**
 * 消息渲染 Hook
 *
 * 渲染策略：parts[] 是唯一数据源（WS 消息和 API 消息均通过 parts 渲染）。
 */
export function useMessageRender(options: UseMessageRenderOptions): MessageRenderContext {
  const { message, isLast = false, isGenerating = false, versionContent } = options

  /** 从 text parts 拼接显示内容；parts 为空时回退到 versionContent 或原始 content */
  const displayContent =
    message.parts && message.parts.length > 0
      ? message.parts
          .filter((p) => p.type === 'text')
          .map((p) => (p as { content: string; text?: string }).content || (p as { content: string; text?: string }).text || '')
          .join('') || message.content
      : versionContent ?? message.content

  /**
   * 从 parts[] 构建渲染片段（唯一路径）
   *
   * 所有消息（WS 流式消息和 API 历史消息）在进入渲染前均已构建 parts[]，
   * 不再需要 contentBlocks 或 content/toolCalls/thinking 的 fallback 路径。
   */
  const fragments = useMemo(() => {
    if (message.parts && message.parts.length > 0) {
      return buildFragmentsFromParts(message)
    }
    return []
  }, [message])

  const isStreaming = useMemo(() => {
    return isGenerating && isLast && message.role === 'assistant'
  }, [isGenerating, isLast, message.role])

  return {
    fragments,
    isStreaming,
    messageId: message.id,
    displayContent,
  }
}

export default useMessageRender
