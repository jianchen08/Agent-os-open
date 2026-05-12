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
  const pipelineId = resolvePipelineId(eventData)
  const messageId = eventData.message_id || eventData.data?.message_id
  _debugLogger.info(
    `[STREAM_START] pipelineId=%s messageId=%s _threadId=%s dataKeys=%s`,
    pipelineId, messageId, eventData._threadId,
    eventData.data ? Object.keys(eventData.data).join(',') : '(no data)',
  )
  if (!pipelineId) return
  if (!messageId) return

  pipelineStore.getState().startStreaming(pipelineId, messageId)

  // BUG-FIX-fix_20260509_tab_streaming:
  // 问题根因: handleStreamStart 未调用 setStreamingForTab，导致 streamingTabs 始终为空，
  //          ChatContainer 的 effectiveIsGenerating 始终为 false，子 Tab 无法显示流式状态。
  // 修复方案: 在流式开始时通过 pipelineId 设置 streaming 状态。
  // 影响范围: 所有标签页的流式指示器和停止按钮。
  useStreamingStore.getState().setStreamingForTab(pipelineId, true)

  // BUG-FIX-fix_20260511_streaming_key_mismatch:
  // 问题根因: router.tsx 和 ChatContainer 使用 sessionId (thread_id) 查找 streamingTabs，
  //          但 setStreamingForTab 只用 pipelineId 作为 key。主管道场景下 pipelineId ≠ thread_id，
  //          导致 effectiveIsGenerating 始终为 true（stream_end 未清除 thread_id 对应的 key），
  //          前端输入框被禁用，用户无法发送第二条消息。
  // 修复方案: 同时用 _threadId 设置 streaming 状态，确保两套 key 都能命中。
  // 影响范围: 主管道多轮对话的输入框可用性。
  // 双 key 设置：主管道场景下 pipelineId（事件携带）可能与 threadId（WS连接标识）不同。
  // ChatContainer 使用 activePipelineId 查找 streaming 状态，
  // 但 activePipelineId 在主管道场景下等于 sessionId/threadId，
  // 所以需要同时设置两个 key 确保 ChatContainer 能正确命中。
  const threadId = eventData._threadId
  if (threadId && threadId !== pipelineId) {
    useStreamingStore.getState().setStreamingForTab(threadId, true)
  }

  resetChunkTimeout(pipelineId, messageId)

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
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id
  const content = eventData.content || eventData.data?.content || ''
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  let msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) {
    _debugLogger.warn(
      `[STREAM_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s totalMsgs=%d _threadId=%s`,
      pipelineId, messageId, msgs.length, eventData._threadId,
    )

    const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
    const nextSeq = existingMsgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1

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

    useStreamingStore.getState().setStreamingForTab(pipelineId, true)

    msg = pipelineStore.getState().getMessages(pipelineId).find((m: any) => m.id === messageId)
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
  }
}

/**
 * 处理流式结束事件
 *
 * 关键修改：不再无条件调用 reconcileContentBlocks，
 * 而是先检查 hasTextBlocks 再决定是否需要对齐。
 */
export function handleStreamEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData._threadId

  // 清理 streaming 状态：优先用 pipelineId，fallback 到 threadId
  const cleanupId = pipelineId || threadId
  if (cleanupId) {
    clearChunkTimeout(cleanupId)
    pipelineStore.getState().stopStreaming(cleanupId)
    useStreamingStore.getState().setStreamingForTab(cleanupId, false)
  }

  // BUG-FIX-fix_20260511_streaming_key_mismatch:
  // 双 key 清理：如果 pipelineId 和 threadId 不同，也需要清理另一个，与 handleStreamStart 中的设置配对。
  if (pipelineId && threadId && pipelineId !== threadId) {
    clearChunkTimeout(threadId)
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }

  if (!pipelineId) return

  const usage = eventData?.usage || eventData?.data?.usage
  if (usage && typeof usage === 'object') {
    useContextUsageStore.getState().updateUsage(pipelineId, usage)
  }

  const messageId = eventData?.message_id || eventData?.data?.message_id
  const fullContent = eventData?.full_content || eventData?.data?.full_content
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
 *
 * BUG-FIX-fix_20260510_streaming_stuck:
 * 当 LLM 调用失败或流式传输异常时，后端发送 stream_error 事件。
 * 前端必须清理该管道的 streaming 状态，否则输入框会一直卡在执行中。
 */
export function handleStreamError(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData._threadId

  _debugLogger.warn(
    `[STREAM_ERROR] pipelineId=%s _threadId=%s`,
    pipelineId, threadId,
  )

  // 清理 streaming 状态：优先用 pipelineId，fallback 到 threadId
  const cleanupId = pipelineId || threadId
  if (cleanupId) {
    clearChunkTimeout(cleanupId)
    pipelineStore.getState().stopStreaming(cleanupId)
    useStreamingStore.getState().stopStreamingForTab(cleanupId)
  }

  // 双 key 清理：如果 pipelineId 和 threadId 不同，也需要清理另一个
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
 * 处理流式保活事件（压缩等长时间操作期间由后端发送）
 */
export function handleStreamKeepalive(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = getChunkTimeoutMessageId(pipelineId)
  if (messageId) {
    resetChunkTimeout(pipelineId, messageId)
  }
}
