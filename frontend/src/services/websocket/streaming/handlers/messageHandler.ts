/**
 * 新消息事件处理器
 *
 * 核心原则：后端持久化数据是唯一真相源。
 * new_message 到达时，用后端数据完全替换流式阶段的临时数据，
 * 确保最终渲染结果与刷新后从 API 加载的数据完全一致。
 * 流式阶段只是多了 streaming 游标用于 UI 动画。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { useStreamingStore } from '@/stores/streamingStore'

import { resolvePipelineId } from '../router'

import { extractMessageId, extractThreadId, terminatePipeline } from './utils'

/**
 * 处理新消息事件
 *
 * 后端持久化完成后的回调。用后端数据完全替换本地流式消息，
 * 保证渲染数据与 API 数据一致。
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
  const finalContent = data?.content || eventData?.content || data?.final_content
  const backendSeq = data?.sequence ?? eventData?.sequence

  console.warn(
    `[MSG-LIFE] ★ new_message 到达: pipeline=%s msgId=%s contentLen=%d seq=%s`,
    pipelineId.slice(0, 12), messageId.slice(0, 12), (finalContent || '').length, backendSeq,
  )

  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    status: 'completed',
    content: finalContent || '',
    ...(backendSeq != null ? { sequence: backendSeq } : {}),
  } as any)
}
