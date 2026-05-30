/**
 * 新消息事件处理器
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline, ensureStreamingPlaceholder } from './utils'

/**
 * 从 API 事件数据中为消息构建 parts[] 数组
 *
 * 当消息尚无 parts 时，根据 content / thinking / toolCalls 构建。
 *
 * @param content - 消息文本内容
 * @param thinking - 思考块数据
 * @param toolCalls - 工具调用列表
 * @returns 构建好的 parts 数组
 */
function buildPartsFromApiData(
  content: string | undefined,
  thinking: any,
  toolCalls: any[] | undefined,
): any[] {
  const parts: any[] = []

  if (thinking?.content && thinking.content.trim()) {
    parts.push({
      type: 'thinking',
      thinking: { content: thinking.content.trim(), isThinking: false },
      state: 'done',
    })
  }

  if (content && content.trim()) {
    parts.push({ type: 'text', content: content.trim(), state: 'done' })
  }

  if (toolCalls && toolCalls.length > 0) {
    for (const tc of toolCalls) {
      parts.push({ type: 'tool_call', toolCall: tc, state: 'done' })
    }
  }

  return parts
}

/**
 * 处理新消息事件
 *
 * 流程：
 * 1. 终止管道并清理流式状态
 * 2. 确保消息存在（复用 ensureStreamingPlaceholder 统一创建占位符）
 * 3. 构建或更新 parts，标记消息为 completed
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  const _diagMsgId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  const _diagData = eventData?.data || eventData
  const _diagSeq = _diagData?.sequence ?? eventData?.sequence
  const _diagContent = eventData?.content || _diagData?.content || _diagData?.final_content
  console.log(
    '[DIAG] handleNewMessage: pipeline=%s msgId=%s sequence=%s content=%.60s ts=%s',
    pipelineId?.slice(0, 12), _diagMsgId?.slice(0, 12), _diagSeq,
    (_diagContent || '').slice(0, 60),
    new Date().toISOString(),
  )

  if (pipelineId) {
    // 标记管道已终止，防止 ensureStreamingPlaceholder 重新启动
    terminatePipeline(pipelineId, threadId)
    // 清理所有关联的 streamingTabs
    const currentActivePipelineId = pipelineStore.getState().activePipelineId
    if (currentActivePipelineId && currentActivePipelineId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(currentActivePipelineId, false)
    }
    if (threadId && threadId !== pipelineId) {
      useStreamingStore.getState().setStreamingForTab(threadId, false)
    }
  } else if (threadId) {
    pipelineStore.getState().stopStreaming(threadId)
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }

  if (!pipelineId) return

  // 统一提取消息 ID
  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  if (!messageId) return

  const finalContent = eventData?.content || eventData?.data?.content || eventData?.data?.final_content
  const data = eventData?.data || eventData
  const backendSeq = data?.sequence ?? eventData?.sequence

  // 确保消息存在：复用 ensureStreamingPlaceholder 统一创建逻辑
  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const existing = msgs.find((m: any) => m.id === messageId)
  if (!existing) {
    const backendSeq = eventData.sequence ?? eventData.data?.sequence
    ensureStreamingPlaceholder(pipelineId, messageId, threadId, backendSeq)
  }

  // 构建已完成消息的 parts 并更新状态
  // BUG-FIX-fix_20260529_minimax_thinking_stream:
  // 当 new_message 未携带 thinking 数据但消息已有流式累积的 parts 时，
  // 不覆盖已有 parts，只更新 status 和 content。
  // 否则流式期间正确接收的 thinking part 会被不含 thinking 的 parts 覆盖掉。
  const builtParts = buildPartsFromApiData(finalContent, data?.thinking, data?.toolCalls)
  const hasExistingParts = existing?.parts && existing.parts.length > 0
  const newMessageHasThinking = !!data?.thinking?.content
  const isAlreadyCompleted = existing?.status === 'completed'

  if (isAlreadyCompleted) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      ...(backendSeq ? { sequence: backendSeq } : {}),
    } as any)
  } else if (hasExistingParts && !newMessageHasThinking && builtParts.length > 0) {
    // 增量更新：保留已有 parts 中的 thinking/tool_call，只更新 text part 的 content
    const mergedParts = existing.parts.map((p: any) => {
      if (p.type === 'text' && finalContent) {
        return { ...p, content: finalContent.trim(), state: 'done' as const }
      }
      return { ...p, state: 'done' as const }
    })
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      content: finalContent,
      ...(backendSeq ? { sequence: backendSeq } : {}),
      parts: mergedParts,
    } as any)
  } else {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      ...(backendSeq ? { sequence: backendSeq } : {}),
      ...(finalContent ? { content: finalContent } : {}),
      ...(builtParts.length > 0 ? { parts: builtParts } : {}),
    } as any)
  }
}
