/** 思考事件处理器（start / chunk / end） 所有 thinking 数据统一走 parts[] 路径，不再维护旧的 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { bufferChunk, flushStreamChunkBuffer } from './streamHandler'
import { ensureStreamingPlaceholder, extractMessageId, extractThreadId } from './utils'

const _debugLogger = loggers.websocket

/** thinking 专属超时（30秒）：超时后自动将 part 状态置为 done 并追加提示 */
const THINKING_TIMEOUT_MS = 30_000

/** 管理所有活跃的 thinking 超时计时器 */
const _thinkingTimeoutMap: Map<string, ReturnType<typeof setTimeout>> = new Map()

/** 清除指定消息的 thinking 超时计时器 */
function clearThinkingTimeout(messageId: string): void {
  const timer = _thinkingTimeoutMap.get(messageId)
  if (timer) {
    clearTimeout(timer)
    _thinkingTimeoutMap.delete(messageId)
  }
}

/** 处理思考开始事件：追加一个新的 thinking part */
export function handleThinkingStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 若 message 不存在（如 stream_start 丢失），先创建占位符
  // 与 handleStreamChunk 保持一致的"有消息就有占位符"语义
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  if (!existingMsgs.find((m: any) => m.id === messageId)) {
    _debugLogger.warn(
      `[THINKING_START] msg not found, auto-creating placeholder: pipeline=%s msgId=%s`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12),
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }

  // 若上一轮 thinking part 仍处于 streaming（thinking_end 丢失/乱序场景），
  // 先兜底把它置为 done，再开新一轮的 thinking part。
  // 这样每次 thinking_start 都对应一个独立卡片，与最终态（每轮 LLM 各一个 part）一致，
  // 避免把两轮思考合并进同一个 part。
  const partIndex = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, 'thinking')
  if (partIndex >= 0) {
    const msgs = pipelineStore.getState().getMessages(pipelineId)
    const msg = msgs.find((m: any) => m.id === messageId)
    const existing = (msg?.parts?.[partIndex] as any)
    if (existing?.state === 'streaming') {
      pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, { state: 'done' })
    }
  }

  // 清除旧的 thinking 超时（如有），启动新的
  clearThinkingTimeout(messageId)
  const timer = setTimeout(() => {
    _thinkingTimeoutMap.delete(messageId)
    _debugLogger.warn('[thinkingHandler] thinking 超时，自动清理: messageId=%s', messageId)
    // 超时后将 part 状态置为 done 并追加提示文本
    const idx = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, 'thinking')
    if (idx >= 0) {
      pipelineStore.getState().appendToPart(pipelineId, messageId, idx, '\n\n⏱ 思考超时，请尝试重新发送')
      pipelineStore.getState().updatePart(pipelineId, messageId, idx, { state: 'done' })
    }
  }, THINKING_TIMEOUT_MS)
  _thinkingTimeoutMap.set(messageId, timer)

  // 通过 parts[] 统一方法追加 thinking part。
  // part 渲染按数组顺序（= 追加顺序 = 接收顺序），不分配 sequence。
  pipelineStore.getState().appendPart(pipelineId, messageId, {
    type: 'thinking',
    content: '',
    state: 'streaming',
  })
}

/** 处理思考块事件：向最后一个 thinking part 追加内容 */
export function handleThinkingChunk(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
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

  // 收到 chunk，清除 thinking 超时（后端仍在响应）
  clearThinkingTimeout(messageId)

  // 若 message 不存在（stream_start 丢失导致），先创建占位符
  // 与 handleStreamChunk 保持一致的"有消息就有占位符"语义
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  if (!existingMsgs.find((m: any) => m.id === messageId)) {
    _debugLogger.warn(
      `[THINKING_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s contentLen=%d`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), chunk.length,
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }

  // 缓冲 chunk，由 RAF 统一刷写（与 stream_chunk 共用批处理机制）。
  // 思考 chunk 若同步写入，每个都触发一次 store 更新 → React 重渲染阻塞主线程，
  // 导致思考"匀速逐字慢"且正文 chunk 积压等主线程空闲才一次性 flush。
  bufferChunk(pipelineId, messageId, chunk, 'thinking')
}

/** 处理思考结束事件：将最后一个 thinking part 状态置为 done */
export function handleThinkingEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 收到 end，清除 thinking 超时
  clearThinkingTimeout(messageId)

  // 先 flush 缓冲区中残留的 thinking chunk，再标记 done，避免末尾内容丢失
  flushStreamChunkBuffer()

  // 通过 parts[] 统一方法将 thinking part 状态置为 done
  const partIndex = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, 'thinking')
  if (partIndex >= 0) {
    pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, {
      state: 'done',
      durationMs: eventData.data?.duration_ms,
    })
  }
}
