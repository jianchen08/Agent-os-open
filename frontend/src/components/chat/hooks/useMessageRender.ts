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
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
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
          const activity = enhanceActivityWithToolConfig(
            toolCallToActivity(block.toolCall),
            block.toolCall,
          )
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
 *
 * 构建顺序: thinking → text → toolCalls（与后端存储的执行顺序一致）
 */
export function buildContentBlocksFromMessage(
  content: string,
  toolCalls: MessageToolCall[] | undefined,
  thinking: Message['thinking'],
  messageId: string,
): ContentBlock[] {
  const blocks: ContentBlock[] = []

  if (thinking && (thinking.content.trim() || thinking.isThinking)) {
    blocks.push({
      type: 'thinking',
      thinking,
      sourceId: messageId,
    })
  }

  // BUG-FIX-fix_20260506_003: 文本块应在 toolCalls 之前
  // 问题根因: toolCalls 放在 text 之前，导致回退路径渲染时工具参数显示在消息气泡外面
  // 修复方案: 调整为 thinking → text → toolCalls 的顺序，与流式构建顺序一致
  if (content && content.trim()) {
    blocks.push({ type: 'text', text: content, sourceId: messageId })
  }

  if (toolCalls) {
    for (const tc of toolCalls) {
      blocks.push({ type: 'tool_call', toolCall: tc, sourceId: messageId })
    }
  }

  return blocks
}

/**
 * 就地校正 contentBlocks 中的文本内容，保留流式构建的交错顺序
 *
 * BUG-FIX-fix_20260507_contentblocks_rebuild:
 * 问题根因: stream_end/new_message 调用 buildContentBlocksFromMessage 从零重建，
 *          把流式期间的交错顺序（thinking→text→tool→text→tool）覆盖为固定顺序（thinking→text→tools），
 *          导致工具卡片位置错乱、文本段合并、部分内容消失。
 * 修复方案: 只更新已有 text block 的内容，保留 tool_call 和 thinking block 不动。
 *
 * @param existingBlocks - 流式期间构建的 contentBlocks
 * @param finalContent - 最终完整文本内容（来自 full_content 或 new_message.content）
 * @param toolCalls - 最新的 toolCalls 列表
 * @param thinking - 最新的 thinking 状态
 * @param messageId - 消息 ID
 * @returns 校正后的 contentBlocks
 */
export function reconcileContentBlocks(
  existingBlocks: ContentBlock[] | undefined,
  finalContent: string,
  toolCalls: MessageToolCall[] | undefined,
  thinking: Message['thinking'],
  messageId: string,
): ContentBlock[] {
  if (!existingBlocks || existingBlocks.length === 0) {
    return buildContentBlocksFromMessage(finalContent, toolCalls, thinking, messageId)
  }

  const textBlocks = existingBlocks.filter((b) => b.type === 'text')
  if (textBlocks.length === 0) {
    if (finalContent?.trim()) {
      return [
        { type: 'text', text: finalContent, sourceId: messageId },
        ...existingBlocks,
      ]
    }
    return [...existingBlocks]
  }

  const textBlockIndices: number[] = []
  existingBlocks.forEach((b, i) => {
    if (b.type === 'text') textBlockIndices.push(i)
  })

  const reconciled = existingBlocks.map((block, i) => {
    if (block.type === 'text' && textBlockIndices.length === 1) {
      return { ...block, text: finalContent, sourceId: messageId }
    }
    if (block.type === 'text' && i === textBlockIndices[textBlockIndices.length - 1]) {
      return { ...block, text: finalContent, sourceId: messageId }
    }
    return block
  })

  return reconciled
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
