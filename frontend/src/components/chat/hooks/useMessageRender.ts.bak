/**
 * 消息渲染 Hook
 *
 * 统一处理消息的渲染上下文
 * 支持文本和工具调用片段的混合渲染
 *
 * 基于 contentBlocks 的有序内容块渲染
 * 渲染顺序由 contentBlocks 决定，保持数据库 sequence 顺序
 */

import { useMemo } from 'react'
import { toolCallToActivity } from '@/utils/activityConverter'
import type { ActivityData } from '@/types/activity'
import type { ContentBlock, Message, MessageToolCall } from '@/types/models'

/**
 * 渲染片段类型
 */
export type RenderFragment =
  | {
      type: 'thinking'
      thinking: import('@/types/models').ThinkingContent
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
 * 从 contentBlocks 构建渲染片段
 */
function buildFragments(contentBlocks: ContentBlock[], messageId: string): RenderFragment[] {
  const fragments: RenderFragment[] = []
  const toolCallCount = contentBlocks.filter((b) => b.type === 'tool_call').length
  let toolCallIndex = 0

  for (const block of contentBlocks) {
    switch (block.type) {
      case 'thinking':
        if (block.thinking) {
          fragments.push({
            type: 'thinking',
            thinking: block.thinking,
            key: `${messageId}-thinking-${block.sourceId || 'cb'}`,
            sourceId: block.sourceId || messageId,
          })
        }
        break

      case 'text':
        if (block.text && block.text.trim()) {
          fragments.push({
            type: 'text',
            content: block.text,
            key: `${messageId}-text-${block.sourceId || 'cb'}-${block.sequence ?? fragments.length}`,
            sourceId: block.sourceId || messageId,
            isLast: false,
          })
        }
        break

      case 'tool_call':
        if (block.toolCall) {
          const activity = toolCallToActivity(block.toolCall)
          fragments.push({
            type: 'tool_call',
            toolCall: block.toolCall,
            activity,
            key: `${messageId}-tool-${block.toolCall.call_id}`,
            index: toolCallIndex,
            total: toolCallCount,
          })
          toolCallIndex++
        }
        break
    }
  }

  if (fragments.length > 0) {
    const lastTextIndex = [...fragments].reverse().findIndex((f) => f.type === 'text')
    if (lastTextIndex !== -1) {
      const idx = fragments.length - 1 - lastTextIndex
      ;(fragments[idx] as { type: 'text'; isLast: boolean }).isLast = true
    }
  }

  return fragments
}

/**
 * 从消息的 content + toolCalls + thinking 动态构建 contentBlocks
 */
function buildContentBlocksFromMessage(
  content: string,
  toolCalls: MessageToolCall[] | undefined,
  thinking: Message['thinking'],
  messageId: string,
): ContentBlock[] {
  const blocks: ContentBlock[] = []

  if (thinking && thinking.content.trim()) {
    blocks.push({
      type: 'thinking',
      thinking,
      sourceId: messageId,
    })
  }

  if (toolCalls) {
    for (const tc of toolCalls) {
      blocks.push({ type: 'tool_call', toolCall: tc, sourceId: messageId })
    }
  }

  if (content && content.trim()) {
    blocks.push({ type: 'text', text: content, sourceId: messageId })
  }

  return blocks
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
 * 渲染策略：
 * - 有 contentBlocks -> 直接使用
 * - 无 contentBlocks -> 从 content + toolCalls + thinking 动态构建
 */
export function useMessageRender(options: UseMessageRenderOptions): MessageRenderContext {
  const { message, isLast = false, isGenerating = false, versionContent } = options

  const displayContent = versionContent ?? message.content

  const fragments = useMemo(() => {
    const blocks =
      message.contentBlocks && message.contentBlocks.length > 0
        ? message.contentBlocks
        : buildContentBlocksFromMessage(
            displayContent,
            message.toolCalls,
            message.thinking,
            message.id,
          )

    return buildFragments(blocks, message.id)
  }, [message, displayContent])

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
