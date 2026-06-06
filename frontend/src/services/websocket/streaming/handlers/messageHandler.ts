/**
 * 新消息事件处理器
 *
 * 后端在 new_message 中携带完整 parts[] 作为权威版本，
 * 前端用其完整替换流式过程中增量构建的消息。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline } from './utils'

/**
 * 处理新消息事件
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    terminatePipeline(pipelineId, threadId)
  } else if (threadId) {
    pipelineStore.getState().stopStreaming(threadId)
    useStreamingStore.getState().setStreamingForTab(threadId, false)
  }

  if (!pipelineId) return

  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  if (!messageId) return

  const data = eventData?.data || eventData
  const serverParts = data?.parts
  const backendSeq = data?.sequence ?? eventData?.sequence

  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  const existingMsg = existingMsgs.find((m: any) => m.id === messageId)

  // 后端发送了完整 parts[] → 用权威版本完整替换
  if (existingMsg && serverParts && Array.isArray(serverParts)) {
    // 仅当 data.content 有值时才更新，避免 null/undefined 覆盖已有内容
    const updatedContent = data?.content != null ? data.content : existingMsg.content
    if (!updatedContent && !serverParts.length) {
      console.warn('[MSG_READY] content 和 parts 均为空，消息将无内容', { messageId, pipelineId })
    }
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      content: updatedContent,
      parts: serverParts,
      status: 'completed',
      sequence: backendSeq ?? existingMsg.sequence,
    } as any)
    return
  }

  // fallback: 仅更新 sequence
  if (existingMsg && backendSeq != null) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      sequence: backendSeq,
    } as any)
  }
}
