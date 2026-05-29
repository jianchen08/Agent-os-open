/**
 * 新消息事件处理器
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline, allocateNextSequence } from './utils'

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
 * 将消息中所有 streaming 状态的 parts 标记为 done
 *
 * @param msg - 消息对象
 * @returns 更新后的 parts 数组
 */
function finalizeStreamingParts(msg: any): any[] {
  return (msg.parts || []).map((p: any) =>
    p.state === 'streaming' ? { ...p, state: 'done' as const } : p,
  )
}

/**
 * 处理新消息事件
 *
 * 流程：
 * 1. 终止管道并清理流式状态
 * 2. 若消息已有 parts 且有文本内容，仅更新 status 并将 streaming parts 改为 done
 * 3. 若消息无 parts 或无文本内容，从 API 数据构建 parts[]
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    // BUG-FIX-fix_20260522: 标记管道已终止（new_message），防止 ensureStreamingPlaceholder 重新启动
    terminatePipeline(pipelineId, threadId)
    // BUG-FIX-fix_20260522_stream_end_over_cleanup:
    // new_message 同样需要清理所有关联的 streamingTabs
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

  // messageHandler 还需要兼容更多消息 ID 来源（event.message?.id, eventData.data?.id）
  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  if (!messageId) return

  const finalContent = eventData?.content || eventData?.data?.content || eventData?.data?.final_content
  const data = eventData?.data || eventData

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  let existing = msgs.find((m: any) => m.id === messageId)

  // BUG-FIX-fix_20260529_new_message_loss:
  // 问题根因: 如果 new_message 事件先于 stream_start 到达，或者占位消息被 initFromAPI 清理了，
  //          existing 为空导致消息被直接丢弃，用户看不到 AI 回复。
  // 修复方案: 当 existing 不存在时，自动创建消息（类似 ensureStreamingPlaceholder 的逻辑），
  //          然后继续执行后续的内容更新流程。
  // 影响范围: new_message 事件处理的消息完整性
  // 修复日期: 2026-05-29
  if (!existing) {
    const sessionId = threadId || pipelineStore.getState().pipelineSessionMap[pipelineId] || ''
    const placeholderSeq = allocateNextSequence(pipelineId)
    pipelineStore.getState().addMessage(pipelineId, {
      id: messageId,
      sessionId,
      role: 'assistant',
      content: finalContent || '',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: placeholderSeq,
      status: 'streaming',
    } as any)
    // 重新获取刚创建的消息
    const updatedMsgs = pipelineStore.getState().getMessages(pipelineId)
    existing = updatedMsgs.find((m: any) => m.id === messageId)
    if (!existing) return
  }

  const existingParts = (existing as any).parts || []
  const hasTextParts = existingParts.some((p: any) => p.type === 'text' && (p.text || p.content)?.trim())

  if (hasTextParts) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      parts: finalizeStreamingParts(existing),
    } as any)
  } else {
    const builtParts = buildPartsFromApiData(finalContent, data?.thinking, data?.toolCalls)
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      ...(finalContent ? { content: finalContent } : {}),
      ...(builtParts.length > 0 ? { parts: builtParts } : {}),
    } as any)
  }
}
