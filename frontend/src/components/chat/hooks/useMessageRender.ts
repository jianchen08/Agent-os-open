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
 * 从 MessageToolCall 构建 ActivityData（legacy 路径专用）
 *
 * @param tc - 工具调用记录
 * @param index - 在 toolCalls 数组中的索引
 * @returns ActivityData 活动数据
 */
function buildActivityFromToolCall(tc: MessageToolCall, index: number): ActivityData {
  return {
    type: 'tool_call',
    id: tc.call_id || `tool-${index}`,
    title: tc.tool_name,
    toolName: tc.tool_name,
    status: tc.status || 'completed',
    durationMs: tc.duration_ms,
    progress: tc.progress,
    currentStep: tc.currentStep,
    details: [],
    error: tc.error,
    actions: [],
  }
}

/**
 * 从 contentBlocks[] 构建渲染片段（API 消息中间格式 fallback）
 *
 * contentBlocks 是后端 API 返回的有序内容块，格式比 parts[] 略旧但仍保留顺序。
 *
 * @param message - 消息对象（必须包含 contentBlocks[]）
 * @returns RenderFragment[] 渲染片段列表
 */
function buildFragmentsFromContentBlocks(message: Message): RenderFragment[] {
  const fragments: RenderFragment[] = []
  const blocks = message.contentBlocks!
  const toolCallCount = blocks.filter((b) => b.type === 'tool_call').length
  let toolCallIndex = 0

  for (let i = 0; i < blocks.length; i++) {
    const block = blocks[i]
    switch (block.type) {
      case 'thinking': {
        if (block.thinking?.content?.trim()) {
          fragments.push({
            type: 'thinking',
            thinking: block.thinking,
            key: `cb-thinking-${i}`,
            sourceId: message.id,
          })
        }
        break
      }
      case 'text': {
        if (block.text?.trim()) {
          fragments.push({
            type: 'text',
            content: block.text,
            key: `cb-text-${i}`,
            sourceId: message.id,
            isLast: false,
          })
        }
        break
      }
      case 'tool_call': {
        if (block.toolCall) {
          const tc = block.toolCall
          const activity = enhanceActivityWithToolConfig(
            buildActivityFromToolCall(tc, i),
            tc,
          )
          fragments.push({
            type: 'tool_call',
            toolCall: tc,
            activity,
            key: `cb-tool-${tc.call_id || i}`,
            index: toolCallIndex,
            total: toolCallCount,
          })
          toolCallIndex++
        }
        break
      }
    }
  }

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
 * 从遗留字段构建渲染片段（API 消息最终 fallback）
 *
 * 当 parts[] 和 contentBlocks[] 均为空时，从 content / toolCalls / thinking
 * 按固定顺序（thinking → text → tool_calls）构建片段。
 * 这确保了页面刷新后从 API 加载的消息仍能正常渲染。
 *
 * @param message - 消息对象（包含 content / toolCalls / thinking 字段）
 * @returns RenderFragment[] 渲染片段列表
 */
function buildFragmentsFromLegacyFields(message: Message): RenderFragment[] {
  const fragments: RenderFragment[] = []

  if (message.thinking?.content?.trim()) {
    fragments.push({
      type: 'thinking',
      thinking: message.thinking,
      key: 'legacy-thinking',
      sourceId: message.id,
    })
  }

  if (message.content?.trim()) {
    fragments.push({
      type: 'text',
      content: message.content,
      key: 'legacy-text',
      sourceId: message.id,
      isLast: true,
    })
  }

  if (message.toolCalls && message.toolCalls.length > 0) {
    const total = message.toolCalls.length
    for (let i = 0; i < message.toolCalls.length; i++) {
      const tc = message.toolCalls[i]
      const activity = enhanceActivityWithToolConfig(
        buildActivityFromToolCall(tc, i),
        tc,
      )
      fragments.push({
        type: 'tool_call',
        toolCall: tc,
        activity,
        key: `legacy-tool-${tc.call_id || i}`,
        index: i,
        total,
      })
    }
  }

  return fragments
}

/**
 * 消息渲染 Hook
 *
 * 渲染策略（优先级从高到低）：
 * 1. parts[] — 流式构建的统一数据源（WS 消息）
 * 2. contentBlocks[] — API 返回的有序内容块
 * 3. content + toolCalls + thinking — API 返回的遗留字段
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
   * 从 parts[] / contentBlocks[] / 遗留字段 构建渲染片段
   *
   * BUG-FIX-fix_20260523_api_msg_not_rendered:
   * 问题根因: 页面刷新后消息从 API 加载，只有 content/toolCalls/thinking 字段，
   *          没有 parts[]。原逻辑在 parts 为空时返回空数组，导致 AI 消息和工具消息
   *          全部返回 null，用户看到空白聊天界面。
   * 修复方案: 添加 contentBlocks 和遗留字段的 fallback 渲染路径，确保 API 消息正常显示。
   * 影响范围: 所有通过 API 加载的历史消息渲染
   * 修复日期: 2026-05-23
   */
  const fragments = useMemo(() => {
    if (message.parts && message.parts.length > 0) {
      return buildFragmentsFromParts(message)
    }
    if (message.contentBlocks && message.contentBlocks.length > 0) {
      return buildFragmentsFromContentBlocks(message)
    }
    return buildFragmentsFromLegacyFields(message)
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
