/**
 * 流式事件处理器（start / chunk / end / error / keepalive）
 */
import { reconcileContentBlocks } from '@/components/chat/hooks/useMessageRender'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'
import { loggers } from '@/utils/logger'

import { clearChunkTimeout, clearPendingStreamTimeout, getChunkTimeoutMessageId, resetChunkTimeout } from '../chunkTimeout'
import { appendTextBlock, appendThinkingChunk } from '../contentBlocks'
import { resolvePipelineId } from '../router'

const _debugLogger = loggers.websocket

/**
 * 处理流式开始事件
 */
export function handleStreamStart(eventData: any) {
  let pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // BUG-FIX-fix_20260513_pipeline_id_silent_drop:
    // 问题根因: 后端事件缺少 pipeline_id 时，resolvePipelineId 返回 null，handler 直接 return 不报错不打日志。
    // 修复方案: 打印 warn 日志，尝试用 _threadId 作为 fallback。
    // 影响范围: 所有缺少 pipeline_id 的流式开始事件
    _debugLogger.warn(
      `[STREAM_START] pipeline_id missing, trying _threadId fallback: _threadId=%s msgId=%s`,
      eventData._threadId?.slice(0, 12),
      (eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id)?.slice(0, 12),
    )
    pipelineId = eventData._threadId || null
  }
  const messageId = eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id
  if (!pipelineId || !messageId) return

  pipelineStore.getState().startStreaming(pipelineId, messageId)
  useStreamingStore.getState().setStreamingForTab(pipelineId, true)

  const threadId = eventData._threadId
  if (threadId && threadId !== pipelineId) {
    useStreamingStore.getState().setStreamingForTab(threadId, true)
  }

  clearPendingStreamTimeout(pipelineId)
  if (threadId && threadId !== pipelineId) {
    clearPendingStreamTimeout(threadId)
  }

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const nextSeq = existingMsgs.reduce((max: number, m: any) => Math.max(max, m.sequence ?? 0), 0) + 1

  pipelineStore.getState().addMessage(pipelineId, {
    id: messageId,
    sessionId: eventData._threadId || '',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    sequence: nextSeq,
    status: 'streaming',
    contentBlocks: [],
  } as any)
}

/**
 * 处理流式块事件
 */
export function handleStreamChunk(eventData: any) {
  let pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    // BUG-FIX-fix_20260513_pipeline_id_silent_drop:
    // 问题根因: 同 handleStreamStart，chunk 事件缺少 pipeline_id 时被静默丢弃。
    // 修复方案: 打印 warn 日志，尝试用 _threadId 作为 fallback。
    // 影响范围: 所有缺少 pipeline_id 的流式块事件
    _debugLogger.warn(
      `[STREAM_CHUNK] pipeline_id missing, trying _threadId fallback: _threadId=%s`,
      eventData._threadId?.slice(0, 12),
    )
    pipelineId = eventData._threadId || null
  }
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id
  const content = eventData.content || eventData.data?.content || eventData.data?.chunk || ''
  if (!messageId) return

  let msgs = pipelineStore.getState().getMessages(pipelineId)
  let msg = msgs.find((m: any) => m.id === messageId)

  if (!msg) {
    // BUG-FIX-fix_20260513_chunk_without_start:
    // 问题根因: PipelineStreamBridge (子管道) 通过 TargetedSink 发送的 stream_start
    // 可能因 WebSocket 竞争或连接状态问题丢失，但后续 stream_chunk 正常到达。
    // 导致消息占位符从未被创建，所有 chunk 被静默丢弃。
    // 修复方案: 收到 chunk 时如果消息不存在，自动创建占位符（和 handleStreamStart 相同逻辑）。
    // 影响范围: 所有子管道流式消息的显示
    _debugLogger.warn(
      `[STREAM_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s totalMsgs=%d`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), msgs.length,
    )
    pipelineStore.getState().startStreaming(pipelineId, messageId)
    useStreamingStore.getState().setStreamingForTab(pipelineId, true)
    const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
    const nextSeq = existingMsgs.reduce((max: number, m: any) => Math.max(max, m.sequence ?? 0), 0) + 1
    pipelineStore.getState().addMessage(pipelineId, {
      id: messageId,
      sessionId: eventData._threadId || '',
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      parentId: null,
      sequence: nextSeq,
      status: 'streaming',
      contentBlocks: [],
    } as any)
    msgs = pipelineStore.getState().getMessages(pipelineId)
    msg = msgs.find((m: any) => m.id === messageId)
    if (!msg) return
  }

  if ((msg as any).thinking?.isThinking) {
    const blocks = appendThinkingChunk(msg.contentBlocks, content)
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      thinking: { content: ((msg as any).thinking.content || '') + content, isThinking: true },
      contentBlocks: blocks,
    } as any)
  } else {
    const blocks = appendTextBlock(msg.contentBlocks, content, messageId)
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      contentBlocks: blocks,
      content: (msg.content || '') + content,
    } as any)
    console.log(
      '%c[CHUNK_OK] pipeline=%s msgId=%s contentLen=%d totalLen=%d',
      'color:green',
      pipelineId?.slice(0, 12), messageId?.slice(0, 12),
      content.length, ((msg.content || '') + content).length,
    )
  }
}

/**
 * 处理流式结束事件
 */
export function handleStreamEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData._threadId

  const cleanupId = pipelineId || threadId
  if (cleanupId) {
    clearChunkTimeout(cleanupId)
    pipelineStore.getState().stopStreaming(cleanupId)
    useStreamingStore.getState().setStreamingForTab(cleanupId, false)
  }

  if (pipelineId && threadId && pipelineId !== threadId) {
    clearChunkTimeout(threadId)
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }

  if (!pipelineId) {
    // BUG-FIX-fix_20260513_pipeline_id_silent_drop:
    // 问题根因: end 事件缺少 pipeline_id 时被静默丢弃。
    // 修复方案: 打印 warn 日志。
    // 影响范围: 所有缺少 pipeline_id 的流式结束事件
    _debugLogger.warn(
      `[STREAM_END] pipeline_id missing, _threadId=%s msgId=%s`,
      eventData._threadId?.slice(0, 12),
      (eventData?.message_id || eventData?.data?.message_id)?.slice(0, 12),
    )
    return
  }

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  const messageId = eventData?.message_id || eventData?.data?.message_id || eventData?.data?.ai_message_id
  const fullContent = eventData?.full_content || eventData?.data?.full_content || eventData?.data?.final_content
  if (!messageId) return

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  const finalContent = fullContent || msg.content || ''
  const finalThinking = (msg as any).thinking
    ? { ...(msg as any).thinking, isThinking: false }
    : undefined

  const existingBlocks = msg.contentBlocks || []
  const hasTextBlocks = existingBlocks.some((b: any) => b.type === 'text' && b.text?.trim())

  let finalBlocks: any[]
  if (hasTextBlocks) {
    finalBlocks = existingBlocks.map((block: any) => {
      if (block.type === 'thinking' && block.thinking) {
        return { ...block, thinking: { ...block.thinking, isThinking: false } }
      }
      return block
    })
  } else if (finalContent.trim()) {
    finalBlocks = reconcileContentBlocks(
      existingBlocks, finalContent, (msg as any).toolCalls, finalThinking, messageId,
    )
  } else {
    finalBlocks = existingBlocks
  }

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    status: 'completed',
    content: finalContent,
    contentBlocks: finalBlocks,
    _reconciled: true,
    ...(finalThinking ? { thinking: finalThinking } : {}),
  } as any)
}

/**
 * 处理流式错误事件
 */
export function handleStreamError(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData._threadId

  const cleanupId = pipelineId || threadId
  if (cleanupId) {
    clearChunkTimeout(cleanupId)
    pipelineStore.getState().stopStreaming(cleanupId)
    useStreamingStore.getState().stopStreamingForTab(cleanupId)
  }

  if (pipelineId && threadId && pipelineId !== threadId) {
    useStreamingStore.getState().stopStreamingForTab(threadId)
  }

  if (!pipelineId) return

  const messageId = eventData?.message_id || eventData?.data?.message_id
  if (messageId) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'error',
    } as any)
  }

  const errorMsg = eventData?.data?.error || eventData?.error || '流式响应异常'
  useNotificationStore.getState().addNotification({
    title: '流式响应错误',
    message: typeof errorMsg === 'string' ? errorMsg : '生成过程中发生错误，请重试',
    priority: 'high',
    category: 'error',
    isBlocking: false,
  })
}

/**
 * 处理流式保活事件
 */
export function handleStreamKeepalive(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = getChunkTimeoutMessageId(pipelineId)
  if (messageId) {
    resetChunkTimeout(pipelineId, messageId)
  }
}
