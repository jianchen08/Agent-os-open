/**
 * 新消息事件处理器
 */
import { reconcileContentBlocks } from '@/components/chat/hooks/useMessageRender'
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'

import { clearChunkTimeout } from '../chunkTimeout'
import { resolvePipelineId } from '../router'

import { extractMessageId, stopPipelineStreaming } from './utils'

/**
 * 处理新消息事件
 *
 * 采用与 handleStreamEnd 相同的 hasTextBlocks 逻辑。
 */
export function handleNewMessage(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  const threadId = eventData.data?._threadId || eventData._threadId

  if (pipelineId) {
    clearChunkTimeout(pipelineId)
    stopPipelineStreaming(pipelineId, threadId)
  } else if (threadId) {
    // FIX: pipeline_id 缺失时仍清理 threadId 的 tab 状态
    clearChunkTimeout(threadId)
    pipelineStore.getState().stopStreaming(threadId)
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
  const existing = msgs.find((m: any) => m.id === messageId)
  if (!existing) return

  if ((existing as any)._reconciled) {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
    } as any)
  } else if (finalContent) {
    const ft = existing.thinking ? { ...existing.thinking, isThinking: false } : undefined
    const existingBlocks = existing.contentBlocks || []
    const hasTextBlocks = existingBlocks.some((b: any) => b.type === 'text' && b.text?.trim())

    let rb: any[]
    if (hasTextBlocks) {
      rb = existingBlocks.map((block: any) => {
        if (block.type === 'thinking' && block.thinking) {
          return { ...block, thinking: { ...block.thinking, isThinking: false } }
        }
        return block
      })
    } else {
      rb = reconcileContentBlocks(existingBlocks, finalContent, existing.toolCalls, ft, messageId)
    }

    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
      content: finalContent,
      contentBlocks: rb,
      _reconciled: true,
      ...(ft ? { thinking: ft } : {}),
    } as any)
  } else {
    pipelineStore.getState().updateMessage(pipelineId, messageId, {
      status: 'completed',
    } as any)
  }
}
