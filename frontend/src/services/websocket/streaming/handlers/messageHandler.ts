/**
 * 新消息事件处理器
 *
 * 后端持久化完成后的确认信号。
 * 仅做 status 确认和 sequence 更新，不覆盖 content（流式阶段已通过 parts[] 完整构建）。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline } from './utils'

/**
 * 处理新消息事件
 *
 * 后端持久化完成后的确认信号。仅做 status 确认和 sequence 更新。
 * 不覆盖 content，流式阶段已通过 parts[] 完整构建了消息内容。
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = extractThreadId(eventData)

  if (pipelineId) {
    terminatePipeline(pipelineId, threadId)
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

  const messageId = extractMessageId(eventData)
    || eventData?.message?.id
    || eventData?.data?.id
  if (!messageId) return

  const data = eventData?.data || eventData
  const backendSeq = data?.sequence ?? eventData?.sequence

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    status: 'completed',
    ...(backendSeq != null ? { sequence: backendSeq } : {}),
  } as any)
}
