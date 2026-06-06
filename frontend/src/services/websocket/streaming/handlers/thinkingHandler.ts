/**
 * 思考事件处理器（start / chunk / end）
 *
 * 所有 thinking 数据统一走 parts[] 路径，不再维护旧的
 * msg.thinking / msg.contentBlocks 兼容字段。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { ensureStreamingPlaceholder, extractMessageId, extractThreadId } from './utils'

const _debugLogger = loggers.websocket

/** thinking 专属超时（30秒）：超时后自动将 part 状态置为 done 并追加提示 */
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
 * 处理思考开始事件：追加一个新的 thinking part
 *
 * BUG-FIX-fix_20260602_thinking_chunk_lost_when_no_placeholder:
 * 问题根因: handleThinkingStart / handleThinkingChunk 依赖 stream_start 先到达创建
 *   message 占位符；当 WS 重连/丢包导致 stream_start 丢失时，
 *   appendPart 静默失败（msgIndex<0），整段 thinking 被静默丢弃，
 *   表现为"WS_RAW 日志正常但页面不渲染"。
 *   同样的问题已被 handleStreamChunk 修复（自动 ensureStreamingPlaceholder），
 *   但 thinkingHandler 没有同步改造。
 * 修复方案: 参照 streamChunk 的兜底语义——"有消息（chunk/start）就有占位符"，
 *   在 handleThinkingStart / handleThinkingChunk 入口先确保 placeholder 存在。
 * 影响范围: 流式思考过程渲染稳定性
 * 修复日期: 2026-06-02
 */
export function handleThinkingStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // FIX: 若 message 不存在（如 stream_start 丢失），先创建占位符
  // 与 handleStreamChunk 保持一致的"有消息就有占位符"语义
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  if (!existingMsgs.find((m: any) => m.id === messageId)) {
    _debugLogger.warn(
      `[THINKING_START] msg not found, auto-creating placeholder: pipeline=%s msgId=%s`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12),
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }

  // 若已存在 streaming 状态的 thinking part，直接跳过
  const partIndex = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, 'thinking')
  if (partIndex >= 0) {
    const msgs = pipelineStore.getState().getMessages(pipelineId)
    const msg = msgs.find((m: any) => m.id === messageId)
    const existing = (msg?.parts?.[partIndex] as any)
    if (existing?.state === 'streaming') return
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

  // 通过 parts[] 统一方法追加 thinking part
  pipelineStore.getState().appendPart(pipelineId, messageId, {
    type: 'thinking',
    content: '',
    state: 'streaming',
    sequence: eventData.data?.sequence ?? Date.now(),
  })
}

/**
 * 处理思考块事件：向最后一个 thinking part 追加内容
 */
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

  // FIX: 若 message 不存在（stream_start 丢失导致），先创建占位符
  // 与 handleStreamChunk 保持一致的"有消息就有占位符"语义
  const existingMsgs = pipelineStore.getState().getMessages(pipelineId)
  if (!existingMsgs.find((m: any) => m.id === messageId)) {
    _debugLogger.warn(
      `[THINKING_CHUNK] msg not found, auto-creating placeholder: pipeline=%s msgId=%s contentLen=%d`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12), chunk.length,
    )
    ensureStreamingPlaceholder(pipelineId, messageId, extractThreadId(eventData))
  }

  // 通过 parts[] 统一方法向最后一个 thinking part 追加内容
  const partIndex = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, 'thinking')
  if (partIndex >= 0) {
    pipelineStore.getState().appendToPart(pipelineId, messageId, partIndex, chunk)
  } else {
    // FIX: thinking part 不存在（start 事件丢失），自动创建再追加
    _debugLogger.warn(
      `[THINKING_CHUNK] thinking part not found, auto-creating: pipeline=%s msgId=%s`,
      pipelineId?.slice(0, 12), messageId?.slice(0, 12),
    )
    pipelineStore.getState().appendPart(pipelineId, messageId, {
      type: 'thinking',
      content: chunk,
      state: 'streaming',
      sequence: eventData.data?.sequence ?? Date.now(),
    })
  }
}

/**
 * 处理思考结束事件：将最后一个 thinking part 状态置为 done
 */
export function handleThinkingEnd(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  // 收到 end，清除 thinking 超时
  clearThinkingTimeout(messageId)

  // 通过 parts[] 统一方法将 thinking part 状态置为 done
  const partIndex = pipelineStore.getState().findLastPartIndex(pipelineId, messageId, 'thinking')
  if (partIndex >= 0) {
    pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, {
      state: 'done',
      durationMs: eventData.data?.duration_ms,
    })
  }
}
