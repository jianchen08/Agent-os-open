/**
 * 思考事件处理器（start / chunk / end）
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'

import { resetChunkTimeout } from '../chunkTimeout'
import { appendThinkingChunk, endThinkingBlock } from '../contentBlocks'
import { resolvePipelineId } from '../router'

/**
 * 处理思考开始事件
 */
export function handleThinkingStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  if ((msg as any).thinking?.isThinking) return
  if ((msg.contentBlocks || []).some((b: any) => b.type === 'thinking' && b.thinking?.isThinking)) return

  const thinkingBlock = { type: 'thinking' as const, thinking: { content: '', isThinking: true } as any, sourceId: messageId }
  const blocks = [...(msg.contentBlocks || []), thinkingBlock]
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: '', isThinking: true },
    contentBlocks: blocks,
  } as any)
}

/**
 * 处理思考块事件
 */
export function handleThinkingChunk(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id
  const chunk = eventData.content || eventData.data?.content || eventData.data?.chunk || ''
  if (!messageId || !chunk) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!(msg as any)?.thinking) return

  const blocks = appendThinkingChunk(msg.contentBlocks, chunk)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: ((msg as any).thinking.content || '') + chunk, isThinking: true },
    contentBlocks: blocks,
  } as any)
}

/**
 * 处理思考结束事件
 */
export function handleThinkingEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = eventData.message_id || eventData.data?.message_id || eventData.data?.ai_message_id
  if (!messageId) return

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!(msg as any)?.thinking) return

  const blocks = endThinkingBlock(msg.contentBlocks)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: (msg as any).thinking.content || '', isThinking: false },
    contentBlocks: blocks,
  } as any)
}
