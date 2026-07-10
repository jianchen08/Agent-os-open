/** 消息渲染 Hook 统一处理消息的渲染上下文 */

import { Copy } from 'lucide-react'
import { useMemo } from 'react'
import { enhanceActivityWithToolConfig, getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { ActivityAction, ActivityData, ActivityDetailBlock } from '@/types/activity'
import type { Message, MessageToolCall, ThinkingContent } from '@/types/models'
import type { MessagePart, SystemLevel, ToolCallPart } from '@/types/messageParts'
/** 渲染片段类型 */
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

/** 渲染上下文 */
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

/** 构建默认的详情区块 */
function buildDefaultDetails(toolCall: MessageToolCall): ActivityDetailBlock[] {
  const details: ActivityDetailBlock[] = []

  details.push({
    id: 'args',
    label: '参数',
    content: toolCall.tool_args,
    contentType: 'json',
    collapsible: true,
    defaultExpanded: false,
  })

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

  if (toolCall.partialOutput && toolCall.partialOutput.length > 0) {
    details.push({
      id: 'output',
      label: '执行输出',
      content: toolCall.partialOutput.join('\n'),
      contentType: 'text',
      collapsible: false,
    })
  }

  return details
}

/** 构建默认的操作按钮 */
function buildDefaultActions(toolCall: MessageToolCall): ActivityAction[] {
  const actions: ActivityAction[] = [
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
    actions.push({
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

  return actions
}

/** 从 ToolCallPart 构建 ActivityData（parts[] 路径专用） */
function buildActivityFromToolPart(
  part: ToolCallPart,
  toolCall: MessageToolCall,
  index: number,
): ActivityData {
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
    details: buildDefaultDetails(toolCall),
    error: part.error,
    actions: buildDefaultActions(toolCall),
  }
}

/** 从 Message.parts[] 构建渲染片段（优先路径） Traverses the parts array and converts each part type into the corresponding RenderFragment. */
/** 生成稳定的 part key，避免数组索引变化导致 React 重新创建 DOM */
function makeStablePartKey(part: MessagePart, index: number): string {
  const seq = part.sequence ?? index
  switch (part.type) {
    case 'text': {
      const content = part.content || (part as any).text || ''
      const contentPrefix = content.substring(0, 16)
      return `part-text-${seq}-${contentPrefix}`
    }
    case 'thinking': {
      const content = part.content || (part as any).thinking?.content || ''
      const contentPrefix = content.substring(0, 16)
      return `part-thinking-${seq}-${contentPrefix}`
    }
    case 'tool_call':
      return `part-tool-${part.callId}`
    case 'system': {
      const contentPrefix = (part.content || '').substring(0, 16)
      return `part-system-${seq}-${contentPrefix}`
    }
    default:
      return `part-${part.type}-${seq}-${index}`
  }
}

export function buildFragmentsFromParts(message: Message, taskId?: string): RenderFragment[] {
  let fragments: RenderFragment[] = []
  const parts = message.parts!

  // part 渲染顺序 = 数组顺序（历史消息 part 已在 API 映射时用 seq++ 保证有序）。
  const toolCallCount = parts.filter((p) => p.type === 'tool_call').length
  let toolCallIndex = 0

  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]
    const stableKey = makeStablePartKey(part, i)
    switch (part.type) {
      case 'text': {
        const textContent = part.content || (part as any).text || ''
        if (textContent && textContent.trim()) {
          fragments.push({
            type: 'text',
            content: textContent,
            key: stableKey,
            sourceId: message.id,
            isLast: false,
          })
        }
        break
      }

      case 'thinking': {
        const thinkContent = part.content || (part as any).thinking?.content || ''
        if (!thinkContent.trim()) break
        fragments.push({
          type: 'thinking',
          thinking: {
            content: thinkContent,
            isThinking: part.state === 'streaming',
            durationMs: part.durationMs,
            steps: part.steps,
          },
          key: stableKey,
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
          resultData: part.resultData,
          error: part.error,
          duration_ms: part.durationMs,
          progress: part.progress,
          currentStep: part.currentStep,
          containerTaskId: part.containerTaskId,
        }
        // 构建 ActivityData 并应用工具卡片注册表增强。
        const activity = enhanceActivityWithToolConfig(
          buildActivityFromToolPart(part, toolCall, i),
          toolCall,
          {
            onOpenFile: (filePath, _recordCtid) =>
              getGlobalOpenFileCallback()(filePath, taskId || _recordCtid),
          },
        )
        fragments.push({
          type: 'tool_call',
          toolCall,
          activity,
          key: stableKey,
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
            key: stableKey,
          })
        }
        break
      }
    }
  }

  // fragments 严格按 parts 数组顺序渲染（= 接收顺序 = 最终态顺序）。
  // 不再做"thinking 整体前置"重排：多轮 LLM 调用的思考应与各自正文交错呈现
  // （思考1→正文1→思考2→正文2），而非把所有思考堆在一起。
  // 后端正常时序已保证单轮内 thinking 在 text 之前。

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

/** Hook 选项 */
export interface UseMessageRenderOptions {
  /** 消息数据 */
  message: Message
  /** 是否为最后一条消息 */
  isLast?: boolean
  /** 是否正在生成 */
  isGenerating?: boolean
  /** 版本内容（编辑时使用） */
  versionContent?: string | null
  /** 当前 Tab 任务 ID，优先作为工具卡片打开文件的工作区解析依据 */
  taskId?: string
}

/** 消息渲染 Hook 渲染策略：parts[] 是唯一数据源（WS 消息和 API 消息均通过 parts 渲染）。 */
export function useMessageRender(options: UseMessageRenderOptions): MessageRenderContext {
  const { message, isLast = false, isGenerating = false, versionContent, taskId } = options

  /** 从 parts[] 构建渲染片段（唯一路径） 所有消息（WS 流式消息和 API 历史消息）在进入渲染前均已构建 parts[]， */
  const { fragments, displayContent } = useMemo(() => {
    if (message.parts && message.parts.length > 0) {
      const frags = buildFragmentsFromParts(message, taskId)
      const textContent = frags
        .filter((f): f is Extract<RenderFragment, { type: 'text' }> => f.type === 'text')
        .map((f) => f.content)
        .join('')
      return {
        fragments: frags,
        displayContent: textContent || message.content,
      }
    }
    return {
      fragments: [],
      displayContent: versionContent ?? message.content,
    }
    // 即使 parts 和 content 没有变化，造成不必要的重渲染。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [message.parts, message.content, versionContent, taskId])

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
