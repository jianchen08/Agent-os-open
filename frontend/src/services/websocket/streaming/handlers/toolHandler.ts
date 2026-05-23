/**
 * 工具调用事件处理器（start / result）
 * 仅使用 parts[] 统一路径，已移除旧 toolCalls / contentBlocks 兼容代码。
 */
import { usePipelineMessageStore as pipelineStore } from '@/stores/pipelineMessageStore'
import { loggers } from '@/utils/logger'

import { resolvePipelineId } from '../router'

import { extractMessageId } from './utils'

const _debugLogger = loggers.websocket

/**
 * 处理工具调用开始事件
 *
 * 向 parts[] 追加一个 tool_call part；若 call_id 已存在则跳过（防重复）。
 */
export function handleToolStart(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) return
  const messageId = extractMessageId(eventData)
  const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  _debugLogger.debug(
    `[TOOL_START] tool=%s callId=%s pipelineId=%s msgId=%s`,
    toolName, callId || '(no-call-id)', pipelineId?.slice(0, 8), messageId?.slice(0, 12),
  )

  const msgs = pipelineStore.getState().getMessages(pipelineId)
  const msg = msgs.find((m: any) => m.id === messageId)
  if (!msg) return

  /* ---- 去重：检查 parts[] 中是否已存在相同 call_id 的 tool_call part ---- */
  const finalCallId = callId || `call_${toolName}_${Date.now()}`
  const parts: any[] = msg.parts || []
  if (parts.some((p: any) => p.type === 'tool_call' && p.callId === finalCallId)) return

  /* ---- 追加 tool_call part ---- */
  pipelineStore.getState().appendPart(pipelineId, messageId, {
    type: 'tool_call',
    callId: finalCallId,
    name: toolName,
    args: eventData.args || eventData.data?.args || eventData.data?.tool_args || {},
    state: 'calling',
    sequence: eventData.sequence ?? eventData.data?.sequence ?? Date.now(),
  })
}

/**
 * 处理工具调用结果事件
 *
 * 在 parts[] 中定位对应的 tool_call part 并更新其状态。
 */
export function handleToolResult(eventData: any) {
  const pipelineId = resolvePipelineId(eventData)
  if (!pipelineId) {
    _debugLogger.warn(
      `[TOOL_RESULT] pipeline_id missing, _threadId=%s msgId=%s tool=%s`,
      eventData.data?._threadId?.slice(0, 12),
      extractMessageId(eventData)?.slice(0, 12),
      eventData.tool_name || eventData.data?.tool_name,
    )
    return
  }
  const messageId = extractMessageId(eventData)
  if (!messageId) return

  const callId = eventData.call_id || eventData.data?.call_id
  if (!callId) return

  /* ---- 通过 call_id 精确匹配 parts[] 中的 tool_call part 并更新 ---- */
  const partIndex = pipelineStore.getState().findToolCallPartIndex(pipelineId, messageId, callId)
  if (partIndex >= 0) {
    pipelineStore.getState().updatePart(pipelineId, messageId, partIndex, {
      state: (eventData.success ?? eventData.data?.success ?? true) === false ? 'error' : 'done',
      result: eventData.result ?? eventData.data?.result,
      error: eventData.error ?? eventData.data?.error,
      durationMs: eventData.duration_ms ?? eventData.data?.duration_ms,
    })
  }
}
