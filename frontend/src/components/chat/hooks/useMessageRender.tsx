/** 消息渲染 Hook 统一处理消息的渲染上下文 */

import { useMemo } from 'react'
import { deriveToolCallActivityStatus } from '@/types/activity'
import { buildDefaultActions } from '@/utils/activityConverter'
import { enhanceActivityWithToolConfig, getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import { expandTruncatedToolResult } from '@/utils/toolResultBackfill'
import {
  buildTruncatedResultDetailBlock,
  resolveTruncatedToolResultForDisplay,
} from '@/utils/toolResultTruncate'
import type { ActivityData, ActivityDetailBlock, ActivityStatus } from '@/types/activity'
import type { MessagePart, SystemLevel, ToolCallPart } from '@/types/messageParts'
import type { Message, MessageToolCall, ThinkingContent } from '@/types/models'
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

/**
 * 工具调用 part 状态推导单一出口（显式终态范式）。
 *
 * fragment 的 toolCall.status 与 activity.status 同源（两处内联映射是复制
 * 漂移温床——'streaming' 曾一处落 pending 一处落 running）。历史孤儿工具
 * 调用（无结果 + 非流式上下文）推导 interrupted 终态，不再由数据缺席
 * 推导 pending/running（永动计时病灶，见 types/activity.ts 推导契约）。
 */
function toolCallPartStatus(
  part: Pick<ToolCallPart, 'state' | 'result' | 'resultData' | 'error'>,
  inStreamingContext: boolean,
): ActivityStatus {
  return deriveToolCallActivityStatus(
    {
      state: part.state,
      hasResult: part.result != null || part.resultData != null || part.state === 'done',
      hasError: part.error != null || part.state === 'error',
    },
    inStreamingContext,
  )
}

/** 从 ToolCallPart 构建 ActivityData（parts[] 路径专用） */
function buildActivityFromToolPart(
  part: ToolCallPart,
  toolCall: MessageToolCall,
  index: number,
  inStreamingContext: boolean,
): ActivityData {
  const status = toolCallPartStatus(part, inStreamingContext)
  return {
    type: 'tool_call',
    id: part.callId || `tool-${index}`,
    title: part.name,
    toolName: part.name,
    status,
    // 中断终态标注（ActivityCard 头部可见；终态卡无计时器无转圈）
    ...(status === 'interrupted' ? { statusText: '已中断（无结果）' as const } : {}),
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
      const content = part.content
      const contentPrefix = content.substring(0, 16)
      return `part-text-${seq}-${contentPrefix}`
    }
    case 'thinking': {
      const content = part.content
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
      return `part-${seq}-${index}`
  }
}

export function buildFragmentsFromParts(
  message: Message,
  taskId?: string,
  opts?: { streaming?: boolean },
): RenderFragment[] {
  const fragments: RenderFragment[] = []
  const parts = message.parts!
  // 活跃流式上下文（显式传入，缺省=非流式）：工具卡状态推导据此区分
  // 「真在途」与「历史孤儿」（无结果工具调用在非流式上下文渲染中断终态）。
  const inStreamingContext = opts?.streaming === true

  // part 渲染顺序 = 数组顺序（历史消息 part 已在 API 映射时用 seq++ 保证有序）。
  const toolCallCount = parts.filter((p) => p.type === 'tool_call').length
  let toolCallIndex = 0

  for (let i = 0; i < parts.length; i++) {
    const part = parts[i]
    const stableKey = makeStablePartKey(part, i)
    switch (part.type) {
      case 'text': {
        const textContent = part.content
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
        const thinkContent = part.content
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
        // 截断标记解析（数据面瘦身批次 1.2）：part 的 result/resultData 超阈值时
        // 是「预览+标记」对象，直接进渲染分支会产出垃圾视图——标记字段剔除、
        // 截断信息收敛为详情块，展开经 expandTruncatedToolResult 单条回取回填。
        const resolved = resolveTruncatedToolResultForDisplay(part.result, part.resultData)
        // 将 ToolCallPart 映射为 MessageToolCall 格式（status 与 activity 同源
        // 推导，见 toolCallPartStatus）
        const toolCall: MessageToolCall = {
          call_id: part.callId,
          tool_name: part.name,
          tool_args: part.args,
          status: toolCallPartStatus(part, inStreamingContext),
          result: resolved.result,
          resultData: resolved.resultData,
          error: part.error,
          duration_ms: part.durationMs,
          progress: part.progress,
          currentStep: part.currentStep,
          partialOutput: part.partialOutput,
          containerTaskId: part.containerTaskId,
        }
        // 构建 ActivityData 并应用工具卡片注册表增强。
        let activity = enhanceActivityWithToolConfig(
          buildActivityFromToolPart(part, toolCall, i, inStreamingContext),
          toolCall,
          {
            onOpenFile: (filePath, _recordCtid) =>
              getGlobalOpenFileCallback()(filePath, taskId || _recordCtid),
          },
        )
        if (resolved.mark) {
          activity = {
            ...activity,
            details: [
              ...(activity.details ?? []),
              buildTruncatedResultDetailBlock(resolved.mark, () =>
                expandTruncatedToolResult({
                  messageId: message.id,
                  partIndex: i,
                  callId: part.callId,
                  sequence: resolved.mark?.sequence,
                }),
              ),
            ],
          }
        }
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

  // 流式判定先于片段构建：工具卡状态推导需要活跃流式上下文（历史孤儿工具
  // 调用在非流式上下文渲染中断终态，见 buildFragmentsFromParts）。
  const isStreaming = useMemo(() => {
    return isGenerating && isLast && message.role === 'assistant'
  }, [isGenerating, isLast, message.role])

  /** 从 parts[] 构建渲染片段（唯一路径） 所有消息（WS 流式消息和 API 历史消息）在进入渲染前均已构建 parts[]， */
  const { fragments, displayContent } = useMemo(() => {
    if (message.parts && message.parts.length > 0) {
      const frags = buildFragmentsFromParts(message, taskId, { streaming: isStreaming })
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
    // content 是 displayContent 的回退源：message.content 与 parts 内文本同源
    // （parts 缺失时 entire content 兜底），依赖缺失会导致编辑/流式更新后
    // 显示旧内容。isStreaming 是工具卡终态推导输入：流式翻转后片段需重建
    // （running→interrupted 收口）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [message.parts, message.content, versionContent, taskId, isStreaming])

  return {
    fragments,
    isStreaming,
    messageId: message.id,
    displayContent,
  }
}

export default useMessageRender
