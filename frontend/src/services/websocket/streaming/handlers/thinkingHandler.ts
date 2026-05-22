/**
 * 思考事件处理器（start / chunk / end）
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resetChunkTimeout } from '../chunkTimeout'
import { appendThinkingChunk, endThinkingBlock } from '../contentBlocks'
import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/** thinking 专属超时（30秒）：超时后自动清理 isThinking 状态 */
const THINKING_TIMEOUT_MS = 30_000

/** 管理所有活跃的 thinking 超时计时器 */
const _thinkingTimeoutMap: Map<string, ReturnType<typeof setTimeout>> = new Map()

/**
 * 清除指定消息的 thinking 超时计时器
 */
function clearThinkingTimeout(messageId: string): void {
  const timer = _thinkingTimeoutMap.get(messageId)
  if (timer) {
    clearTimeout(timer)
    _thinkingTimeoutMap.delete(messageId)
  }
}

/**
 * 处理思考开始事件
 */
export function handleThinkingStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  resetChunkTimeout(pipelineId, messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  if ((msg as any).thinking?.isThinking) return
  if ((msg.contentBlocks || []).some((b: any) => b.type === 'thinking' && b.thinking?.isThinking)) return

  // 清除旧的 thinking 超时（如有），启动新的
  clearThinkingTimeout(messageId)
  const timer = setTimeout(() => {
    _thinkingTimeoutMap.delete(messageId)
    _debugLogger.warn('[thinkingHandler] thinking 超时，自动清理: messageId=%s', messageId)
    // 自动清理 isThinking 状态并显示超时提示
    const currentMsgs = pipelineStore.getState().getMessages(pipelineId)
    const currentMsg = currentMsgs.find((m: any) => m.id === messageId)
    if (currentMsg && (currentMsg as any).thinking?.isThinking) {
      pipelineStore.getState().updateMessage(pipelineId, messageId, {
        thinking: { ...(currentMsg as any).thinking, content: ((currentMsg as any).thinking.content || '') + '\n\n⏱ 思考超时，请尝试重新发送', isThinking: false },
      } as any)
    }
  }, THINKING_TIMEOUT_MS)
  _thinkingTimeoutMap.set(messageId, timer)

  const sequence = eventData.sequence ?? eventData.data?.sequence
  const thinkingBlock = { type: 'thinking' as const, thinking: { content: '', isThinking: true } as any, sourceId: messageId, sequence }
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
  if (!pipelineId) {
    // FIX: pipeline_id 缺失时记录 warn
    _debugLogger.warn(
      `[THINKING_CHUNK] pipeline_id missing, _threadId=%s msgId=%s`,
      eventData.data?._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
    )
    return
  }
  const messageId = extractMessageId(eventData)
  const chunk = eventData.content || eventData.data?.content || eventData.data?.chunk || ''
  if (!messageId || !chunk) return

  resetChunkTimeout(pipelineId, messageId)

  // 收到 chunk，清除 thinking 超时（后端仍在响应）
  clearThinkingTimeout(messageId)

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
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 收到 end，清除 thinking 超时
  clearThinkingTimeout(messageId)

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!(msg as any)?.thinking) return

  const blocks = endThinkingBlock(msg.contentBlocks)
  pipelineStore.getState().updateMessage(pipelineId, messageId, {
    thinking: { content: (msg as any).thinking.content || '', isThinking: false },
    contentBlocks: blocks,
  } as any)
}
